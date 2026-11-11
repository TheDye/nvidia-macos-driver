#!/usr/bin/env python3
"""Measure Chrome's software A/V clock alignment and decoder use with an audio+video fixture.

Usage: python3 tools/bench_chrome_av_sync.py /path/to/av264.mp4 --out /tmp/av-report --transitions
Use a known synchronized 1080p60 video with AAC audio (e.g. white flash + beep at each second).
A disposable, visible Chrome profile plays quietly, never changes your real profile, flags, or driver, then closes.
requestVideoFrameCallback timestamps and HTMLMediaElement.currentTime are SOFTWARE timing, not a measurement of
speaker/display latency or lip sync. Verify physical A/V with an external recording of the flash+beep fixture.
--transitions also measures after pause/resume, seek, and 1.25x playback. A fresh GPU-process sample proves active
NVDEC only when decoder stack frames are present; loaded libraries/capabilities alone are not counted as proof.
"""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

PAGE = r'''<!doctype html><meta charset="utf-8"><title>A/V software clock probe</title>
<style>html,body{margin:0;background:#000}video{width:1600px;height:900px}</style>
<video id="v" playsinline loop></video><script>
const config=__CONFIG__,v=document.getElementById('v');v.src=config.src;v.volume=0.08;
const events=[],results=[];let active=null,finished=false;
const wait=ms=>new Promise(r=>setTimeout(r,ms));
function quantile(a,p){if(!a.length)return null;const b=[...a].sort((x,y)=>x-y);return +b[Math.min(b.length-1,Math.floor(p*b.length))].toFixed(3)}
for(const e of ['waiting','stalled','seeking','seeked','pause','playing','ratechange','error'])v.addEventListener(e,()=>{
 events.push({event:e,wall_ms:+performance.now().toFixed(1),media_s:v.currentTime});
 if(active&&['seeking','seeked','waiting','playing','ratechange'].includes(e)){active.settle=performance.now()+500;active.prevFrame=null;active.prevRaf=null}
});
function videoFrame(now,m){
 const a=active;
 if(a&&now>a.settle&&!v.seeking&&!v.paused&&v.currentTime>0.5){
  // Project currentTime's media clock to Chrome's expected display time; do NOT treat it as a speaker timestamp.
  const mediaAtDisplay=v.currentTime+(m.expectedDisplayTime-now)*v.playbackRate/1000;
  a.offsets.push((m.mediaTime-mediaAtDisplay)*1000);
  if(a.prevFrame!==null)a.callbackGaps+=Math.max(0,m.presentedFrames-a.prevFrame-1);
  a.prevFrame=m.presentedFrames;
 }else if(a)a.prevFrame=null;
 if(!finished)v.requestVideoFrameCallback(videoFrame);
}
function animation(t){const a=active;if(a&&t>a.settle&&!v.paused){if(a.prevRaf!==null)a.raf.push(t-a.prevRaf);a.prevRaf=t}else if(a)a.prevRaf=null;if(!finished)requestAnimationFrame(animation)}
async function measure(name){
 await wait(3000);
 const q=v.getVideoPlaybackQuality();
 const a={name,offsets:[],raf:[],callbackGaps:0,prevFrame:null,prevRaf:null,settle:performance.now(),start:performance.now(),mediaStart:v.currentTime};active=a;
 await wait(config.seconds*1000);active=null;
 const end=v.getVideoPlaybackQuality(),n=a.offsets.length,k=Math.floor(n/3);
 results.push({name,rate:v.playbackRate,samples:n,software_offset_p05_ms:quantile(a.offsets,.05),software_offset_p50_ms:quantile(a.offsets,.5),software_offset_p95_ms:quantile(a.offsets,.95),
  first_third_offset_ms:quantile(a.offsets.slice(0,k),.5),last_third_offset_ms:quantile(a.offsets.slice(n-k),.5),
  missed_video_callbacks:a.callbackGaps,dropped_frames:end.droppedVideoFrames-q.droppedVideoFrames,total_frames:end.totalVideoFrames-q.totalVideoFrames,
  raf_p50_ms:quantile(a.raf,.5),raf_p95_ms:quantile(a.raf,.95),raf_over12:a.raf.filter(x=>x>12).length,
  audio_decoded_bytes:Number.isFinite(v.webkitAudioDecodedByteCount)?v.webkitAudioDecodedByteCount:null,
  elapsed_s:(performance.now()-a.start)/1000,muted:v.muted,volume:v.volume,paused:v.paused,media_error:v.error?.code??null});
}
async function run(){
 try{
  await v.play();v.requestVideoFrameCallback(videoFrame);requestAnimationFrame(animation);await measure('steady');
  if(config.transitions){v.pause();await wait(1000);await v.play();await measure('pause-resume');
   await new Promise((resolve,reject)=>{v.addEventListener('seeked',resolve,{once:true});v.addEventListener('error',reject,{once:true});v.currentTime=Math.min(5,v.duration/3)});await measure('seek');
   v.playbackRate=1.25;await measure('rate-1.25');}
  finished=true;v.pause();console.log('AVREPORT '+JSON.stringify({scope:'software clock only; no physical lip-sync proof',results,events}));
 }catch(e){finished=true;console.log('AVREPORT '+JSON.stringify({error:String(e),media_error:v.error?.code??null,events}))}
}
run();</script>'''


def cpu_seconds(pid):
    text = subprocess.check_output(['ps', '-o', 'cputime=', '-p', str(pid)], text=True).strip()
    day, _, clock = text.rpartition('-')
    parts = list(map(float, clock.split(':')))
    value = sum(v * 60 ** i for i, v in enumerate(reversed(parts)))
    return value + (int(day) * 86400 if day else 0)


def gpu_pid(profile):
    text = subprocess.check_output(['ps', '-axo', 'pid=,args='], text=True)
    for line in text.splitlines():
        args = line.split(None, 1)
        if len(args) == 2 and '--type=gpu-process' in args[1] and ('--user-data-dir=' + str(profile)) in args[1].split():
            return int(args[0])
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('video', type=Path)
    p.add_argument('--out', type=Path, required=True, help='new or empty output directory')
    p.add_argument('--chrome', type=Path, default=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'))
    p.add_argument('--seconds', type=int, default=12, help='measurement seconds per phase, after 3 s warm-up')
    p.add_argument('--transitions', action='store_true')
    a = p.parse_args()
    if not a.video.is_file() or not a.chrome.is_file():
        p.error('video/Chrome executable not found')
    if not 5 <= a.seconds <= 300:
        p.error('--seconds must be 5..300')
    if a.out.exists() and any(a.out.iterdir()):
        p.error('--out must be new or empty (never overwrites reports)')
    a.out.mkdir(parents=True, exist_ok=True)
    out = a.out.resolve()
    profile = Path(tempfile.mkdtemp(prefix='nm-av-'))
    cfg = json.dumps({'src': a.video.resolve().as_uri(), 'seconds': a.seconds, 'transitions': a.transitions}).replace('<', '\\u003c')
    page = profile / 'probe.html'
    page.write_text(PAGE.replace('__CONFIG__', cfg))
    (out / 'probe.html').write_text(page.read_text())
    sample_file = out / 'gpu.sample.txt'
    report = {'video': str(a.video.resolve()), 'chrome': str(a.chrome), 'scope': 'software clocks; no physical audio/display calibration'}
    command = [str(a.chrome), '--user-data-dir=' + str(profile), '--no-first-run', '--no-default-browser-check',
               '--autoplay-policy=no-user-gesture-required', '--window-position=0,0', '--window-size=1700,1000',
               '--enable-logging=stderr', '--v=0', page.as_uri()]
    log = out / 'chrome.log'
    proc = None
    sampler = None
    try:
        with log.open('w') as f:
            proc = subprocess.Popen(command, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + (a.seconds + 4) * (4 if a.transitions else 1) + 35
            sample_after = time.monotonic() + 7
            cpustart = None
            while time.monotonic() < deadline:
                if cpustart is None and time.monotonic() >= sample_after:
                    pid = gpu_pid(profile)
                    if pid:
                        cpustart = (pid, cpu_seconds(pid), time.monotonic())
                        sampler = subprocess.Popen(['sample', str(pid), '2', '-f', str(sample_file)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                text = log.read_text(errors='replace')
                matches = re.findall(r'AVREPORT (\{[^\n]+\})', text)
                if matches:
                    report['playback'] = json.loads(matches[-1])
                    break
                if proc.poll() is not None:
                    report['error'] = 'Chrome exited before reporting'
                    break
                time.sleep(0.25)
            else:
                report['error'] = 'playback/report timeout'
            if cpustart:
                pid, cpu0, wall0 = cpustart
                try:
                    report['gpu_process_cpu_percent_one_core'] = round((cpu_seconds(pid) - cpu0) * 100 / (time.monotonic() - wall0), 2)
                except subprocess.CalledProcessError:
                    report['gpu_process_cpu_percent_one_core'] = None
            if sampler:
                sampler.wait(timeout=15)
            sample = sample_file.read_text(errors='replace') if sample_file.exists() else ''
            report['active_nvdec_symbols'] = sorted(set(re.findall(r'\b(nvd26[45]_[a-zA-Z0-9_]+)\s+\(in ', sample)))
            report['hardware_decode_proven_by_sample'] = bool(report['active_nvdec_symbols'])
    finally:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=8)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
        if sampler and sampler.poll() is None:
            sampler.terminate()
            sampler.wait(timeout=5)
        # Keep the disposable profile for investigating timeout/errors, but it is never re-used.
        report['disposable_profile'] = str(profile)
    (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    playback = report.get('playback', {})
    return int(bool(report.get('error') or playback.get('error') or not playback.get('results') or
                    any(x['samples'] < 10 or x['muted'] or x['media_error'] for x in playback.get('results', []))))


if __name__ == '__main__':
    raise SystemExit(main())
