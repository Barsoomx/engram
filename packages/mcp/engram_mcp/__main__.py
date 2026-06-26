from engram_mcp.client import create_memory_link, fetch_context, search_memory
from engram_mcp.server import ToolMap, run_server


def build_tools() -> ToolMap:
    return {
        'engram_search': search_memory,
        'engram_context': fetch_context,
        'engram_memory_link': create_memory_link,
    }


if __name__ == '__main__':
    run_server(build_tools())
