#!/usr/bin/env python3
"""Offline checks for the Chrome A/V probe; no user browser or audio touched."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('av_probe', ROOT / 'tools/bench_chrome_av_sync.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeTests(unittest.TestCase):
    def test_cpu_time_units(self):
        for text, expected in [('0:03.50', 3.5), ('1:02:03', 3723), ('2-01:02:03', 176523)]:
            with self.subTest(text=text), patch.object(probe.subprocess, 'check_output', return_value=text):
                self.assertEqual(probe.cpu_seconds(12), expected)

    def test_only_own_gpu_process_is_sampled(self):
        profile = Path('/tmp/unique-av-profile')
        table = '\n'.join([
            '100 /Applications/Google Chrome --type=gpu-process --user-data-dir=/tmp/unique-av-profile-other',
            '101 /Applications/Google Chrome --user-data-dir=/tmp/unique-av-profile',
            '102 /Applications/Google Chrome Helper --type=gpu-process --user-data-dir=/tmp/unique-av-profile',
        ])
        with patch.object(probe.subprocess, 'check_output', return_value=table):
            self.assertEqual(probe.gpu_pid(profile), 102)
        with patch.object(probe.subprocess, 'check_output', return_value=table.replace('--type=gpu-process', '--type=renderer')):
            self.assertIsNone(probe.gpu_pid(profile))

    def test_software_clock_projection_and_callback_gaps(self):
        js = probe.PAGE.split('<script>', 1)[1].split('</script>', 1)[0].replace('__CONFIG__', '{}')
        pre = '''const fakeVideo={currentTime:2,playbackRate:1,paused:false,seeking:false,addEventListener(){},
 play(){return new Promise(()=>{})},requestVideoFrameCallback(){}};
const document={getElementById(){return fakeVideo}},performance={now(){return 0}};
'''
        post = '''
active={offsets:[],raf:[],settle:0,prevFrame:null,callbackGaps:0};
videoFrame(1000,{mediaTime:2,expectedDisplayTime:1010,presentedFrames:3});
videoFrame(1001,{mediaTime:2,expectedDisplayTime:1011,presentedFrames:6});
v.playbackRate=1.25;videoFrame(1002,{mediaTime:2,expectedDisplayTime:1012,presentedFrames:7});
active.settle=10000;videoFrame(1003,{mediaTime:2,expectedDisplayTime:1013,presentedFrames:8});
console.log(JSON.stringify(active));
'''
        r = subprocess.run(['node', '-e', pre + js + post], text=True, capture_output=True, check=True)
        a = json.loads(r.stdout)
        self.assertEqual(len(a['offsets']), 3)
        self.assertAlmostEqual(a['offsets'][0], -10)
        self.assertAlmostEqual(a['offsets'][2], -12.5)
        self.assertEqual(a['callbackGaps'], 2)
        self.assertIsNone(a['prevFrame'])
        self.assertIn('no physical lip-sync proof', probe.PAGE)
        self.assertIn('end.droppedVideoFrames-q.droppedVideoFrames', probe.PAGE)


if __name__ == '__main__':
    unittest.main(verbosity=2)
