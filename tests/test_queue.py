import ast
import asyncio
from pathlib import Path
import unittest
import re
from urllib.parse import unquote_plus

ROOT = Path(__file__).resolve().parent
SERVER = ROOT / 'server.py'
if not SERVER.exists():
    SERVER = ROOT.parent / 'scripts/server.py'

def functions(path):
    tree = ast.parse(path.read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and (n.name.startswith('_queue') or n.name in ('get_queue', 'format_job', 'get_download_queue', 'format_size'))]
    for n in nodes:
        n.decorator_list = []
    ns = {'Path': Path, 're': re, 'asyncio': asyncio, 'unquote_plus': unquote_plus}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), ns)
    return ns

class QueueTests(unittest.TestCase):
    def test_offline_job_is_visible_with_unknown_telemetry(self):
        ns = functions(SERVER)
        self.assertIn('_queue_item', ns, 'queue normalizer must exist')
        item = ns['_queue_item']({'job_id': 'one', 'optional_name': 'Fireman Sam S10E01'}, None, 'RPC unavailable')
        self.assertEqual(item['name'], 'Fireman Sam S10E01')
        self.assertEqual(item['status'], 'stopped')
        self.assertIsNone(item['progress']['downloaded'])
        self.assertIsNone(item['download_speed'])
        self.assertEqual(item['error'], 'RPC unavailable')

    def test_endpoint_and_live_statuses(self):
        ns = functions(SERVER)
        self.assertIn('get_queue', ns)
        ns['_load_torrent_status_jobs'] = lambda: {}
        result = asyncio.run(ns['get_queue']())
        self.assertEqual(result['jobs'], [])
        for raw, seeder, expected in [('active', 'false', 'downloading'), ('active', 'true', 'seeding'), ('complete', 'false', 'completed'), ('error', 'false', 'stopped')]:
            item = ns['_queue_item']({}, {'status': raw, 'seeder': seeder, 'totalLength': '100', 'completedLength': '50', 'downloadSpeed': '2', 'uploadSpeed': '1'})
            self.assertEqual(item['status'], expected)
            self.assertEqual(item['progress']['percent'], 50)

    def test_dashboard_formats_unknown_without_fake_zero(self):
        path = ROOT / 'dock-dashboard.py'
        if not path.exists():
            path = ROOT.parent / 'scripts/dock-dashboard.py'
        ns = functions(path)
        item = ns['format_job']({'name': 'Fireman Sam S09E17 Turtle Hunt', 'status': 'stopped', 'progress': {'downloaded': None, 'total': None, 'percent': None}, 'error': 'offline'})
        self.assertEqual(item['Progress'], '—')
        self.assertEqual(item['Status'], 'stopped')
        self.assertEqual(item['Error'], 'offline')

    def test_rpc_payload_and_metadata_filter(self):
        from io import BytesIO
        from types import SimpleNamespace
        ns = functions(SERVER)
        class FakePath:
            def __init__(self, value):
                self.value = value
                self.name = Path(value).name
            def open(self, mode):
                return BytesIO(b'Download GID#abc not complete: [METADATA]Fireman+Sam\n')
            def read_bytes(self):
                return b'aria2c\0--log\0/downloads/job.log\0'
        ns['Path'] = FakePath
        ns['httpx'] = SimpleNamespace(HTTPError=RuntimeError)
        calls = []
        class Client:
            async def post(self, url, json):
                calls.append(json['method'])
                rows = [{'status': 'active', 'gid': 'payload', 'totalLength': '100', 'completedLength': '25', 'bittorrent': {'info': {'name': 'Fireman Sam'}}}] if json['method'].endswith('tellActive') else ([{'status': 'complete', 'followedBy': ['payload']}] if json['method'].endswith('tellStopped') else [])
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'result': rows})
        job = {'pid': 1, 'rpc_port': 6800, 'job_id': 'a', 'log_file': '/downloads/job.log'}
        rows = asyncio.run(ns['_queue_job'](Client(), job))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['progress']['percent'], 25)
        self.assertEqual(rows[0]['name'], 'Fireman Sam')
        self.assertEqual(calls, ['aria2.tellActive', 'aria2.tellWaiting', 'aria2.tellStopped'])
        self.assertNotIn('name', job)

if __name__ == '__main__':
    unittest.main()
