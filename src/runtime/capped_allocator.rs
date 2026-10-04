//! A V8 `ArrayBuffer` allocator with a hard byte budget.
//!
//! V8 charges `ArrayBuffer` / `SharedArrayBuffer` backing stores to the
//! embedder's allocator, not to the JS heap, so `max_heap_size` alone leaves
//! gigabytes of host RAM reachable from guest code. This allocator refuses any
//! request that would push live bytes past `cap`; V8 then throws a catchable
//! `RangeError: Array buffer allocation failed` instead of the host swapping or
//! being OOM-killed.
//!
//! The budget is *live* bytes: `free` returns what `allocate` took, so a guest
//! that drops its buffers can allocate again.
//!
//! V8 does not route *resizable* `ArrayBuffer`s (nor growable
//! `SharedArrayBuffer`s) through this allocator: their backing stores come from
//! its page allocator. The bridge JS in `ops.rs` charges those to the same
//! [`Budget`] through `op_pydeno_buffer_reserve` / `op_pydeno_buffer_release`,
//! which is why the budget is shared (`Arc`) rather than owned by the allocator.

use std::alloc::{alloc, alloc_zeroed, dealloc, Layout};
use std::cell::RefCell;
use std::ffi::c_void;
use std::ptr;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;

use deno_core::v8;

// ponytail: 16 covers every typed-array element size V8 hands the allocator; raise if it ever asks for more.
const ALIGN: usize = 16;

/// A live-byte budget with a hard cap, shared by the allocator and the bridge.
pub struct Budget {
    live: AtomicUsize,
    cap: usize,
    /// Set right before the allocator refuses a backing store. V8 then collects garbage and, as
    /// the last resort of that same allocation attempt, invokes the near-heap-limit callback;
    /// the callback takes this flag to tell "the budget said no" (a catchable RangeError for the
    /// guest, the heap is fine) from a heap that really is at its limit (terminate).
    refused: AtomicBool,
}

impl Budget {
    pub fn new(cap: usize) -> Arc<Self> {
        Arc::new(Self {
            live: AtomicUsize::new(0),
            cap,
            refused: AtomicBool::new(false),
        })
    }

    /// Whether the allocator has just refused a backing store; cleared by the read.
    pub fn take_refusal(&self) -> bool {
        self.refused.swap(false, Ordering::AcqRel)
    }

    /// Reserve `len` bytes, or `false` if that would exceed the cap.
    pub fn reserve(&self, len: usize) -> bool {
        self.live
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |live| {
                live.checked_add(len).filter(|total| *total <= self.cap)
            })
            .is_ok()
    }

    pub fn release(&self, len: usize) {
        // Saturating: a stray double release must not wrap `live` past `cap`
        // and refuse every later allocation.
        let _ = self
            .live
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |live| {
                Some(live.saturating_sub(len))
            });
    }
}

fn layout(len: usize) -> Option<Layout> {
    // V8 never asks for zero bytes through this path, but `Layout` rejects it.
    Layout::from_size_align(len.max(1), ALIGN).ok()
}

/// The refusal-path hook: given the budget, release what collected resizable buffers held.
pub type Sweeper = Box<dyn Fn(&Budget)>;

thread_local! {
    /// What the runtime on this thread wants run before a reservation is refused: the bridge's
    /// sweep of resizable buffers V8 has collected, whose bytes the budget still holds. V8 calls
    /// the allocator on the isolate thread, and when it gets `null` back it collects garbage and
    /// asks again, so the handles of dropped resizable buffers are empty by the retry; without
    /// this hook a fixed-length allocation could fail on bytes nobody holds any more.
    static SWEEPER: RefCell<Option<Sweeper>> = const { RefCell::new(None) };
}

/// Install the refusal-path sweeper for the runtime on this thread (one runtime per thread).
pub fn set_thread_sweeper(sweeper: Sweeper) {
    SWEEPER.with(|slot| *slot.borrow_mut() = Some(sweeper));
}

/// Reserve `len`, sweeping once and retrying before refusing.
fn reserve_or_sweep(budget: &Budget, len: usize) -> bool {
    // A stale flag from an earlier refusal is cleared by the next request, so it can only be
    // read by the callback V8 invokes within the refused allocation itself.
    budget.refused.store(false, Ordering::Release);
    if budget.reserve(len) {
        return true;
    }
    // `try_borrow`: the sweeper is never installed re-entrantly, but a refusal must not panic.
    SWEEPER.with(|slot| {
        if let Ok(slot) = slot.try_borrow() {
            if let Some(sweeper) = slot.as_ref() {
                sweeper(budget);
            }
        }
    });
    if budget.reserve(len) {
        return true;
    }
    budget.refused.store(true, Ordering::Release);
    false
}

fn take(budget: &Budget, len: usize, zeroed: bool) -> *mut c_void {
    let Some(layout) = layout(len) else {
        return ptr::null_mut();
    };
    if !reserve_or_sweep(budget, len) {
        return ptr::null_mut();
    }
    // SAFETY: `layout` has a non-zero size.
    let p = unsafe {
        if zeroed {
            alloc_zeroed(layout)
        } else {
            alloc(layout)
        }
    };
    if p.is_null() {
        budget.release(len);
    }
    p.cast()
}

unsafe extern "C" fn allocate(budget: &Budget, len: usize) -> *mut c_void {
    take(budget, len, true)
}

unsafe extern "C" fn allocate_uninitialized(budget: &Budget, len: usize) -> *mut c_void {
    take(budget, len, false)
}

unsafe extern "C" fn free(budget: &Budget, data: *mut c_void, len: usize) {
    if data.is_null() {
        return;
    }
    if let Some(layout) = layout(len) {
        // SAFETY: V8 only frees pointers this allocator returned, with the same `len`.
        unsafe { dealloc(data.cast(), layout) };
        budget.release(len);
    }
}

unsafe extern "C" fn drop_budget(budget: *const Budget) {
    // SAFETY: `budget` came from `Arc::into_raw` in `new`.
    drop(unsafe { std::sync::Arc::from_raw(budget) });
}

static VTABLE: v8::RustAllocatorVtable<Budget> = v8::RustAllocatorVtable {
    allocate,
    allocate_uninitialized,
    free,
    drop: drop_budget,
};

/// An allocator that charges every backing store it hands out to `budget`.
pub fn new(budget: Arc<Budget>) -> v8::UniqueRef<v8::Allocator> {
    // SAFETY: the handle is an `Arc<Budget>` raw pointer and `VTABLE` matches `Budget`.
    unsafe { v8::new_rust_allocator(Arc::into_raw(budget), &VTABLE) }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reserve_respects_cap_and_release_restores_it() {
        let b = Budget {
            live: AtomicUsize::new(0),
            cap: 100,
            refused: AtomicBool::new(false),
        };
        assert!(b.reserve(60));
        assert!(!b.reserve(41));
        assert!(b.reserve(40));
        b.release(60);
        assert!(b.reserve(60));
    }

    #[test]
    fn take_refuses_over_cap_and_free_returns_budget() {
        let b = Budget {
            live: AtomicUsize::new(0),
            cap: 4096,
            refused: AtomicBool::new(false),
        };
        let first = take(&b, 4096, true);
        assert!(!first.is_null());
        // zeroed, as `allocate` promises
        assert!(
            unsafe { std::slice::from_raw_parts(first.cast::<u8>(), 4096) }
                .iter()
                .all(|byte| *byte == 0)
        );
        assert!(take(&b, 1, true).is_null(), "budget is exhausted");
        unsafe { free(&b, first, 4096) };
        let again = take(&b, 4096, false);
        assert!(!again.is_null(), "freeing returned the budget");
        unsafe { free(&b, again, 4096) };
    }

    #[test]
    fn free_of_null_and_double_release_are_harmless() {
        let b = Budget {
            live: AtomicUsize::new(0),
            cap: 100,
            refused: AtomicBool::new(false),
        };
        unsafe { free(&b, ptr::null_mut(), 50) };
        b.release(10);
        assert_eq!(
            b.live.load(Ordering::Acquire),
            0,
            "release saturates at zero"
        );
        assert!(b.reserve(100));
    }

    #[test]
    fn reserve_does_not_overflow() {
        let b = Budget {
            live: AtomicUsize::new(1),
            cap: usize::MAX,
            refused: AtomicBool::new(false),
        };
        assert!(!b.reserve(usize::MAX));
    }
}
