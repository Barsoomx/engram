from engram_mcp.client import (
    create_memory_link,
    fetch_context,
    list_observations,
    search_memory,
    update_memory_version,
)
from engram_mcp.server import ToolMap, run_server


def build_tools() -> ToolMap:
    return {
        'engram_search': search_memory,
        'engram_context': fetch_context,
        'engram_memory_link': create_memory_link,
        'engram_observations': list_observations,
        'engram_memory_version': update_memory_version,
    }


if __name__ == '__main__':
    run_server(build_tools())
