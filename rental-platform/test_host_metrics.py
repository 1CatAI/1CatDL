import csv
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from host_metrics import HostMetrics, _parse_dimm_info, _parse_power_sample
from remote_backend import BackendRouter
import server


class HostMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.power = self.root / 'power'
        (self.power / 'history').mkdir(parents=True)
        (self.proc / 'cpuinfo').write_text(
            'processor : 0\nphysical id : 0\nmodel name : Intel(R) Xeon(R) Test CPU\n\n'
            'processor : 1\nphysical id : 1\nmodel name : Intel(R) Xeon(R) Test CPU\n', encoding='utf-8')
        (self.proc / 'stat').write_text('cpu 100 0 100 800 0 0 0 0\n', encoding='utf-8')
        (self.proc / 'meminfo').write_text(
            'MemTotal: 100000 kB\nMemAvailable: 40000 kB\nSwapTotal: 1000 kB\nSwapFree: 250 kB\n',
            encoding='utf-8')
        self.now = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc).timestamp()
        (self.root / 'power.conf').write_text('RATE_CNY_PER_KWH=1.10\n', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def collector(self, now=None):
        completed = SimpleNamespace(returncode=0, stdout='''Memory Device
        Size: 32 GB
        Configured Memory Speed: 3200 MT/s
Memory Device
        Size: No Module Installed
Memory Device
        Size: 32 GB
        Configured Memory Speed: 3200 MT/s
''')
        with patch('host_metrics.subprocess.run', return_value=completed):
            return HostMetrics(self.proc, self.power / 'state.json', self.power / 'history',
                               self.root / 'power.conf', clock=lambda: self.now if now is None else now)

    def write_power(self):
        rows = []
        for offset, watts, message in ((-20, 1000, 'first sample; integration starts at the next successful sample'),
                                       (-10, 2000, 'integrated by trapezoidal rule'),
                                       (0, 1000, 'integrated by trapezoidal rule')):
            epoch = self.now + offset
            rows.append({'timestamp': datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='seconds'),
                         'status': 'ok', 'watts': str(watts), 'source': 'ipmi-dcmi',
                         'interval_seconds': '10', 'added_kwh': '0', 'total_kwh': '0',
                         'rate_cny_per_kwh': '1.1', 'cost_cny_at_current_rate': '0', 'message': message})
        path = self.power / 'history' / datetime.fromtimestamp(self.now, timezone.utc).date().isoformat()
        with (path.with_suffix('.csv')).open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader(); writer.writerows(rows)
        state = {'last_sample_epoch': self.now, 'last_watts': 1000, 'last_source': 'ipmi-dcmi',
                 'first_sample_at': rows[0]['timestamp'], 'last_sample_at': rows[-1]['timestamp'],
                 'sample_interval_seconds': 10, 'total_kwh': 2.5, 'samples_ok': 3,
                 'samples_failed': 0, 'gaps': 0}
        (self.power / 'state.json').write_text(json.dumps(state), encoding='utf-8')

    def test_host_resource_snapshot_and_cpu_delta(self):
        metrics = self.collector()
        (self.proc / 'stat').write_text('cpu 150 0 150 900 0 0 0 0\n', encoding='utf-8')
        value = metrics.snapshot()
        self.assertEqual(value['cpu']['model'], '2 × Intel Xeon Test CPU')
        self.assertEqual((value['cpu']['sockets'], value['cpu']['threads'], value['cpu']['usagePct']), (2, 2, 50.0))
        self.assertEqual(value['memory']['totalBytes'], 100000 * 1024)
        self.assertEqual((value['memory']['usagePct'], value['memory']['speedMTs'], value['memory']['modules']), (60.0, 3200, 2))
        self.assertEqual((value['swap']['usedBytes'], value['swap']['usagePct']), (750 * 1024, 75.0))

    def test_power_window_cost_and_forecast_use_bmc_history(self):
        self.write_power()
        power = self.collector(now=self.now + 5).snapshot()['power']
        self.assertEqual(power['status'], 'live')
        self.assertEqual(power['source'], 'ipmi-dcmi')
        self.assertEqual(power['currentW'], 1000.0)
        self.assertEqual(power['average24hW'], 1500.0)
        self.assertEqual(power['coverageSeconds24h'], 20.0)
        self.assertAlmostEqual(power['cost24hCny'], 0.0092, places=4)
        self.assertEqual(power['totalCostCny'], 2.75)
        self.assertEqual(power['forecast30dCny'], 1188.0)
        self.assertEqual(len(power['history24h']), 3)

    def test_missing_meter_is_explicitly_collecting(self):
        power = self.collector().snapshot()['power']
        self.assertEqual(power['status'], 'collecting')
        self.assertIsNone(power['currentW'])
        self.assertIsNone(power['forecast30dCny'])

    def test_old_sample_is_stale_not_silently_live(self):
        self.write_power()
        power = self.collector(now=self.now + 120).snapshot()['power']
        self.assertEqual(power['status'], 'stale')
        self.assertEqual(power['sampleAgeSeconds'], 120.0)

    def test_confirmed_off_chassis_zero_watts_is_valid_history(self):
        sample = _parse_power_sample({'status': 'ok',
            'timestamp': datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
            'watts': '0', 'message': 'integrated by trapezoidal rule'})
        self.assertIsNotNone(sample)
        self.assertEqual(sample.watts, 0.0)

    def test_dimm_parser_ignores_empty_slots_and_uses_populated_speed(self):
        speed, count = _parse_dimm_info('''Memory Device
 Size: 64 GB
 Speed: 2933 MT/s
 Configured Memory Speed: 2933 MT/s
Memory Device
 Size: No Module Installed
''')
        self.assertEqual((speed, count), (2933, 1))

    def test_offline_node_still_has_centrally_collected_bmc_power(self):
        central_dir = self.root / 'nodes' / 'G2-003'
        central_dir.mkdir(parents=True)
        (central_dir / 'state.json').write_text('{}', encoding='utf-8')
        service = server.Service.__new__(server.Service)
        service.local_node_id = 'G2-002'
        service.core = SimpleNamespace(nodes={'G2-002': {}, 'G2-003': {}})
        service.backend = BackendRouter(None, [{'id': 'G2-003'}], 'G2-002')
        service.lock = threading.RLock()
        service.power_node_root = self.root / 'nodes'
        service.remote_power_metrics = {}
        meter = Mock()
        meter.power_snapshot.return_value = {'status': 'live', 'currentW': 40.0}
        with patch.object(server, 'SIMULATION', False), patch.object(server, 'HostMetrics', return_value=meter):
            telemetry = service.host_telemetry('G2-003')
        self.assertEqual(telemetry['status'], 'unavailable')
        self.assertEqual(telemetry['power'], {'status': 'live', 'currentW': 40.0})
        meter.power_snapshot.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
