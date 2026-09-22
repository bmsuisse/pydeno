"""
FastAPI multi-tenant JavaScript execution with resource limits and error handling.

This example demonstrates:
- Per-tenant runtime isolation
- Resource limits (memory, execution time)
- Comprehensive error handling
- Runtime statistics monitoring
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from pydeno import JavaScriptError, Runtime, RuntimeConfig


class EvalRequest(BaseModel):
    code: str
    timeout: float = 5.0  # Default 5 second timeout


class EvalResponse(BaseModel):
    tenant: str
    result: str | None = None
    error: str | None = None
    error_type: str | None = None


# Per-tenant runtime storage
_tenants: dict[str, Runtime] = {}

# Configure resource limits for safety
TENANT_CONFIG = RuntimeConfig(
    max_heap_size=50 * 1024 * 1024,  # 50MB heap limit per tenant
    timeout=10.0,  # Global max timeout (can be overridden per request)
)


def get_runtime(tenant_id: str) -> Runtime:
    """Get or create a runtime for a tenant with resource limits."""
    runtime = _tenants.get(tenant_id)
    if runtime is None or runtime.is_closed():
        runtime = Runtime(TENANT_CONFIG)
        _tenants[tenant_id] = runtime
    return runtime


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Cleanup all tenant runtimes on shutdown."""
    try:
        yield
    finally:
        print(f"Shutting down {len(_tenants)} tenant runtimes...")
        while _tenants:
            tenant_id, runtime = _tenants.popitem()
            runtime.close()
            print(f"  Closed runtime for tenant: {tenant_id}")


app = FastAPI(title="pydeno multi-tenant playground", lifespan=lifespan)


@app.post("/tenants/{tenant_id}/eval", response_model=EvalResponse)
async def eval_js(tenant_id: str, request: EvalRequest):
    """
    Execute JavaScript code for a specific tenant.

    Each tenant gets an isolated V8 runtime with:
    - 50MB heap limit
    - Configurable timeout per request
    - Persistent state across evaluations
    """
    runtime = get_runtime(tenant_id)

    try:
        # Use the lower of request timeout and global max
        timeout = min(request.timeout, TENANT_CONFIG.timeout or 10.0)
        result = await runtime.eval_async(request.code, timeout=timeout)

        return EvalResponse(
            tenant=tenant_id,
            result=str(result),
        )

    except TimeoutError:
        # JavaScript execution exceeded timeout
        return EvalResponse(
            tenant=tenant_id,
            error=f"Execution exceeded {request.timeout}s timeout",
            error_type="TimeoutError",
        )

    except JavaScriptError as e:
        # JavaScript threw an error (syntax error, runtime error, etc.)
        return EvalResponse(
            tenant=tenant_id,
            error=str(e),
            error_type="JavaScriptError",
        )

    except Exception as e:
        # Other runtime errors
        return EvalResponse(
            tenant=tenant_id,
            error=str(e),
            error_type=type(e).__name__,
        )


@app.post("/tenants/{tenant_id}/fib")
async def compute_fib(tenant_id: str, n: int):
    """Compute Fibonacci number (example of sync evaluation)."""
    if n < 0 or n > 40:  # Prevent extremely slow computations
        raise HTTPException(status_code=400, detail="n must be between 0 and 40")

    runtime = get_runtime(tenant_id)
    script = f"""
        (function fib(n) {{
            return n < 2 ? n : fib(n - 1) + fib(n - 2);
        }})({n})
    """

    try:
        result = runtime.eval(script)
        return {"tenant": tenant_id, "n": n, "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/tenants/{tenant_id}/stats")
async def get_stats(tenant_id: str):
    """Get runtime statistics for a tenant."""
    runtime = _tenants.get(tenant_id)
    if runtime is None or runtime.is_closed():
        raise HTTPException(
            status_code=404, detail=f"No active runtime for tenant {tenant_id}"
        )

    stats = runtime.get_stats()
    return {
        "tenant": tenant_id,
        "stats": stats,
    }


@app.delete("/tenants/{tenant_id}")
async def delete_tenant(tenant_id: str):
    """Clean up a tenant's runtime."""
    runtime = _tenants.pop(tenant_id, None)
    if runtime:
        runtime.close()
        return {"tenant": tenant_id, "status": "deleted"}
    raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")


@app.get("/tenants")
async def list_tenants():
    """List all active tenants."""
    return {
        "count": len(_tenants),
        "tenants": list(_tenants.keys()),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
