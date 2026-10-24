"""Exercise an installed Collector from the official independent MCP client.

Uses a fresh directory and synthetic records; no phone, DAXQO server or Motor.
"""
import argparse
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check(python):
    with tempfile.TemporaryDirectory(prefix="collector-external-host-") as temporary:
        params = StdioServerParameters(command=python, args=[
            "-m", "trajectory.mcp_server", "--data", str(Path(temporary) / "data"),
            "--device-id", "b683d45a-e298-40b3-90c2-3bcd8f59cb84",
            "--session-id", "7468c2e4-dae4-4d3b-b959-b6e671f6f951", "--source", "independent_mcp_host"],
            cwd=temporary, env={"PYTHONUTF8": "1"})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=20)) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                assert names == {"trajectory_record", "trajectory_stats", "trajectory_export"}, names
                async def call(name, arguments):
                    result = await session.call_tool(name, arguments)
                    assert not result.isError, result
                    return json.loads(result.content[0].text)
                action = {"task_id": "independent-task", "action_id": "independent-action",
                    "action": {"type": "click", "parameters": {}},
                    "before": {"package_name": "demo.app", "nodes": [{"text": "Details"}]},
                    "after": {"package_name": "demo.app", "nodes": [{"text": "Demo details"}]},
                    "status": "succeeded", "started_at": "2026-10-24T10:00:00Z",
                    "finished_at": "2026-10-24T10:00:01Z"}
                assert (await call("trajectory_record", action))["inserted"] == 1
                assert (await call("trajectory_record", action))["duplicates"] == 1
                stats = await call("trajectory_stats", {})
                assert stats["records"] == 1, stats
                await call("trajectory_export", {"name": "sample"})
                assert (Path(temporary) / "data/exports/sample/manifest.json").is_file()
                print(json.dumps({"host": "official MCP Python SDK", "server": initialized.serverInfo.name,
                    "protocol": initialized.protocolVersion, "records": stats["records"],
                    "retry_deduplicated": True, "export_verified": True, "data": "synthetic"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="Python containing the installed server package")
    asyncio.run(check(parser.parse_args().python))
