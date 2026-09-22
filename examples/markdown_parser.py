"""
Parse Markdown using JavaScript from Python.

This example demonstrates:
- Loading external JavaScript libraries from CDN (unpkg)
- Exposing JavaScript function to Python
- Practical integration of JS ecosystem tools in Python

We use marked.js, a popular markdown parser, to convert markdown to HTML.
"""

import asyncio
import httpx
from pydeno import Runtime


async def load_marked_library() -> str:
    """Fetch marked.js from unpkg CDN."""
    url = "https://unpkg.com/marked@12/marked.min.js"

    print(f"Fetching marked.js from {url}...")
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        print(f"Downloaded {len(response.text)} bytes\n")
        return response.text


async def main() -> None:
    """Demonstrate parsing Markdown via JavaScript."""

    marked_js = await load_marked_library()

    with Runtime() as runtime:
        runtime.eval(marked_js)
        print("Marked loaded into runtime\n")

        parse_markdown = runtime.eval(
            """
            function parseMarkdown(markdown, options = {}) {
                try {
                    // Configure marked if needed
                    if (options.gfm !== undefined) {
                        marked.setOptions({ gfm: options.gfm });
                    }

                    // Parse the markdown
                    const html = marked.parse(markdown);

                    return {
                        success: true,
                        html: html,
                        length: html.length
                    };
                } catch (error) {
                    return {
                        success: false,
                        error: error.message
                    };
                }
            };
            parseMarkdown
            """
        )

        print("=== Parsing Markdown ===\n")
        markdown = """
# Hello from pydeno!

This is **bold** and this is *italic*.

- Item 1
- Item 2
- Item 3
        """

        result = parse_markdown(markdown)
        if result["success"]:
            print(f"✓ Parsed markdown ({result['length']} bytes)\n")
            print(f"HTML output:\n{result['html']}")
        else:
            print(f"✗ Error: {result['error']}")


if __name__ == "__main__":
    asyncio.run(main())
