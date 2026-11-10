#!/usr/bin/env python3
"""The ANGLE Metal rule is on for Chromium-family and WebKit GPU processes; other processes stay opt-in.

The constructor read the environment and the process name; lift it into a fixture with stubs so the
matching and the NVMTL_ANGLE_METAL overrides are checked without a GPU or a browser.
"""
import pathlib
import subprocess
import tempfile

# Functional mode is opt-in: no browser is launched by the normal policy regression.
# Requires Node >=22 (built-in WebSocket), Chrome/Chromium, and red-left/blue-right
# h264.mp4 + hevc.mp4 fixtures. No packages, installs, profile edits or GPU flags.
CHROME_FUNCTIONAL_JS = r'''
import fs from 'node:fs';
import http from 'node:http';
import {spawn, execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
const [chromePath, mediaDir, profile, output] = process.argv.slice(1);
const evidence = {started: new Date().toISOString(), tests: {}, mediaEvents: [], errors: [],
  requestedICD: process.env.NVMTL_ICD_LIB || null, hardwareDecodeProven: false};
const delay = ms => new Promise(r => setTimeout(r, ms));
const html = '<!doctype html><meta charset=utf-8><title>Chrome functional acceleration probe</title><style>body{height:4000px;margin:0}#box{position:fixed;top:150px;left:20px;width:80px;height:80px;background:red;will-change:transform}canvas,video{width:128px;height:96px}</style><div id=box></div><canvas id=canvas width=128 height=96></canvas>';
const server = http.createServer((req, res) => {
  if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); res.end(html); }
  else if (['/h264.mp4', '/hevc.mp4'].includes(req.url)) {
    res.setHeader('Content-Type', 'video/mp4'); res.end(fs.readFileSync(mediaDir + req.url));
  } else { res.writeHead(404); res.end(); }
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const origin = 'http://127.0.0.1:' + server.address().port;
const log = fs.openSync(output + '.chrome.log', 'w');
const chrome = spawn(chromePath, ['--user-data-dir=' + profile, '--remote-debugging-port=0',
  '--no-first-run', '--no-default-browser-check', '--disable-component-update',
  '--disable-background-networking', '--disable-sync',
  '--disable-features=OptimizationGuideOnDeviceModel', '--autoplay-policy=no-user-gesture-required',
  '--window-size=900,700', origin], {stdio: ['ignore', log, log]});
const sockets = [];
process.on('SIGTERM', () => { chrome.kill('SIGTERM'); server.close(); process.exit(143); });
async function connect(url) {
  const ws = new WebSocket(url); sockets.push(ws);
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
  let id = 0; const pending = new Map();
  ws.onmessage = event => {
    const m = JSON.parse(event.data);
    if (m.id) { const p = pending.get(m.id); if (p) { pending.delete(m.id); m.error ? p[1](Error(JSON.stringify(m.error))) : p[0](m.result); } }
    else if (m.method?.startsWith('Media.')) evidence.mediaEvents.push(m);
  };
  return (method, params = {}) => new Promise((r, j) => {
    const i = ++id; pending.set(i, [r, j]); ws.send(JSON.stringify({id:i, method, params}));
    setTimeout(() => { if (pending.delete(i)) j(Error('CDP timeout: ' + method)); }, 60000).unref();
  });
}
try {
  let active;
  for (let i=0; i<300; i++) { try { active=fs.readFileSync(profile+'/DevToolsActivePort','utf8').split('\n'); break; } catch {} await delay(100); }
  if (!active) throw Error('Chrome CDP startup timed out');
  const base='http://127.0.0.1:'+active[0];
  const browser=await connect('ws://127.0.0.1:'+active[0]+active[1]);
  evidence.version=await browser('Browser.getVersion'); evidence.system=await browser('SystemInfo.getInfo');
  const tabs=await (await fetch(base+'/json')).json();
  const cdp=await connect(tabs.find(t=>t.url.startsWith(origin)).webSocketDebuggerUrl);
  await cdp('Runtime.enable'); await cdp('Page.enable'); await cdp('Media.enable');
  async function evaluate(fn) {
    const r=await cdp('Runtime.evaluate',{expression:'('+fn.toString()+')()',awaitPromise:true,returnByValue:true});
    if (r.exceptionDetails) throw Error(JSON.stringify(r.exceptionDetails)); return r.result.value;
  }
  const internal=await browser('Target.createTarget',{url:'chrome://media-internals/',background:true});
  await delay(500);
  const all=await (await fetch(base+'/json')).json();
  const icdp=await connect(all.find(t=>t.id===internal.targetId).webSocketDebuggerUrl);
  const windowInfo=await browser('Browser.getWindowForTarget',{targetId:tabs.find(t=>t.url.startsWith(origin)).id});
  await browser('Browser.setWindowBounds',{windowId:windowInfo.windowId,bounds:{windowState:'normal'}});
  await browser('Browser.setWindowBounds',{windowId:windowInfo.windowId,bounds:{left:0,top:30,width:900,height:650}});
  await cdp('Page.bringToFront');
  await delay(500);
  evidence.window=await browser('Browser.getWindowBounds',{windowId:windowInfo.windowId});
  evidence.screen=await evaluate(()=>({visibility:document.visibilityState,focused:document.hasFocus(),x:screenX,y:screenY,width:outerWidth,height:outerHeight,screen:{width:screen.width,height:screen.height,availLeft:screen.availLeft,availTop:screen.availTop,availWidth:screen.availWidth,availHeight:screen.availHeight},pixelRatio:devicePixelRatio}));
  evidence.tests.webgl=await evaluate(async()=>{
    const results=[]; const near=(a,b)=>a.every((v,i)=>Math.abs(v-b[i])<=2);
    for (const kind of ['webgl','webgl2']) {
      const r={kind}; const c=document.createElement('canvas'); c.width=c.height=16; document.body.append(c);
      try {
        const g=c.getContext(kind,{preserveDrawingBuffer:true,antialias:false}); if (!g) throw Error('context unavailable');
        const ext=g.getExtension('WEBGL_debug_renderer_info'); r.renderer=g.getParameter(ext?ext.UNMASKED_RENDERER_WEBGL:g.RENDERER);
        function setup() {
          function shader(type,text) { const s=g.createShader(type); g.shaderSource(s,text); g.compileShader(s); if(!g.getShaderParameter(s,g.COMPILE_STATUS))throw Error(g.getShaderInfoLog(s)); return s; }
          const p=g.createProgram();
          g.attachShader(p,shader(g.VERTEX_SHADER,'attribute vec2 p;varying vec2 uv;void main(){gl_Position=vec4(p,0.,1.);uv=p*.5+.5;}'));
          g.attachShader(p,shader(g.FRAGMENT_SHADER,'precision mediump float;varying vec2 uv;uniform sampler2D tex;uniform vec4 color;void main(){gl_FragColor=texture2D(tex,uv)*color;}'));
          g.linkProgram(p); if(!g.getProgramParameter(p,g.LINK_STATUS))throw Error(g.getProgramInfoLog(p)); g.useProgram(p);
          g.bindBuffer(g.ARRAY_BUFFER,g.createBuffer()); g.bufferData(g.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),g.STATIC_DRAW);
          const a=g.getAttribLocation(p,'p'); g.enableVertexAttribArray(a); g.vertexAttribPointer(a,2,g.FLOAT,false,0,0);
          texture(new Uint8Array([128,255,64,255]),1,1); g.uniform4f(g.getUniformLocation(p,'color'),1,1,1,1); return p;
        }
        function texture(data,w,h) { const t=g.createTexture(); g.bindTexture(g.TEXTURE_2D,t); g.texImage2D(g.TEXTURE_2D,0,g.RGBA,w,h,0,g.RGBA,g.UNSIGNED_BYTE,data); g.texParameteri(g.TEXTURE_2D,g.TEXTURE_MIN_FILTER,g.NEAREST); g.texParameteri(g.TEXTURE_2D,g.TEXTURE_MAG_FILTER,g.NEAREST); return t; }
        function draw() { g.drawArrays(g.TRIANGLES,0,3); const a=new Uint8Array(4); g.readPixels(4,4,1,1,g.RGBA,g.UNSIGNED_BYTE,a); return [...a]; }
        let p=setup(); g.viewport(0,0,16,16); r.shaderTexture=draw();
        const t=texture(null,16,16), f=g.createFramebuffer(); g.bindFramebuffer(g.FRAMEBUFFER,f); g.framebufferTexture2D(g.FRAMEBUFFER,g.COLOR_ATTACHMENT0,g.TEXTURE_2D,t,0);
        r.framebufferComplete=g.checkFramebufferStatus(g.FRAMEBUFFER)===g.FRAMEBUFFER_COMPLETE;
        texture(new Uint8Array([128,255,64,255]),1,1); // no attachment/sampler feedback
        g.clearColor(0,0,1,1); g.clear(g.COLOR_BUFFER_BIT); g.enable(g.BLEND); g.blendFunc(g.SRC_ALPHA,g.ONE_MINUS_SRC_ALPHA); g.uniform4f(g.getUniformLocation(p,'color'),1,1,1,.5); r.framebufferBlend=draw();
        g.disable(g.BLEND); g.bindFramebuffer(g.FRAMEBUFFER,null); c.width=32;c.height=24;g.viewport(0,0,32,24);g.uniform4f(g.getUniformLocation(p,'color'),1,1,1,1);r.resize=draw();r.glError=g.getError();
        const lose=g.getExtension('WEBGL_lose_context'); if(!lose)throw Error('lifecycle extension missing');
        await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('context restore timeout')),10000);c.addEventListener('webglcontextlost',e=>{e.preventDefault();setTimeout(()=>lose.restoreContext(),100);},{once:true});c.addEventListener('webglcontextrestored',()=>{clearTimeout(timer);resolve();},{once:true});lose.loseContext();});
        p=setup();g.viewport(0,0,32,24);r.restored=draw();
        r.pass=r.framebufferComplete&&r.glError===0&&near(r.shaderTexture,[128,255,64,255])&&near(r.framebufferBlend,[64,128,160,191])&&near(r.resize,[128,255,64,255])&&near(r.restored,[128,255,64,255]);
      }catch(e){r.pass=false;r.error=String(e);}finally{c.remove();}results.push(r);
    }return results;
  });
  evidence.tests.webgpu=await evaluate(async()=>{
    try {const a=await navigator.gpu.requestAdapter();if(!a)throw Error('no adapter');const d=await a.requestDevice();
      const b=d.createBuffer({size:16,usage:GPUBufferUsage.STORAGE|GPUBufferUsage.COPY_SRC}),r=d.createBuffer({size:16,usage:GPUBufferUsage.COPY_DST|GPUBufferUsage.MAP_READ});
      const p=d.createComputePipeline({layout:'auto',compute:{entryPoint:'main',module:d.createShaderModule({code:'@group(0) @binding(0) var<storage,read_write> a:array<u32>;@compute @workgroup_size(1) fn main(@builtin(global_invocation_id) id:vec3u){a[id.x]=id.x*7u+3u;}'})}});
      const e=d.createCommandEncoder(),c=e.beginComputePass();c.setPipeline(p);c.setBindGroup(0,d.createBindGroup({layout:p.getBindGroupLayout(0),entries:[{binding:0,resource:{buffer:b}}]}));c.dispatchWorkgroups(4);c.end();e.copyBufferToBuffer(b,0,r,0,16);d.queue.submit([e.finish()]);await r.mapAsync(GPUMapMode.READ);const values=[...new Uint32Array(r.getMappedRange())];r.unmap();d.destroy();return {values,pass:values.join(',')==='3,10,17,24'};
    }catch(e){return {pass:false,error:String(e)};}
  });
  await delay(1500); // exclude shader cold-start from the cadence observation
  evidence.tests.elements=await evaluate(async()=>{
    const c=document.getElementById('canvas'),x=c.getContext('2d'),box=document.getElementById('box');x.fillStyle='#0f0';x.fillRect(0,0,128,96);const pixel=[...x.getImageData(32,32,1,1).data],times=[];
    await new Promise(resolve=>{let n=0;function frame(t){times.push(t);box.style.transform='translateX('+(n%200)+'px)';window.scrollTo(0,n*2);x.fillStyle=n%2?'red':'lime';x.fillRect(0,0,10,10);if(++n<240)requestAnimationFrame(frame);else resolve();}requestAnimationFrame(frame);});
    const gaps=times.slice(1).map((t,i)=>t-times[i]).sort((a,b)=>a-b);
    return {pass:pixel.join(',')==='0,255,0,255'&&scrollY>0&&box.getBoundingClientRect().x>20,pixel,scrollY,boxX:box.getBoundingClientRect().x,frames:times.length,elapsedMs:times.at(-1)-times[0],p50Ms:gaps[Math.floor(gaps.length*.5)],p95Ms:gaps[Math.floor(gaps.length*.95)],maxMs:gaps.at(-1),cadenceNote:'observed callbacks, not physical display latency or a high-refresh guarantee'};
  });
  evidence.tests.video=await evaluate(async()=>{
    const results=[];window.probeVideos=[];window.scrollTo(0,0);
    for(const codec of ['h264','hevc']){const r={codec},v=document.createElement('video');v.muted=true;v.src='/'+codec+'.mp4';document.body.append(v);window.probeVideos.push(v);
      try{await Promise.race([v.play(),new Promise((_,j)=>setTimeout(()=>j(Error('play timeout')),15000))]);await new Promise((resolve,reject)=>{const t=setTimeout(()=>reject(Error('frame timeout')),10000);v.requestVideoFrameCallback(()=>{clearTimeout(t);resolve();});});await new Promise(r=>setTimeout(r,400));
        const c=document.createElement('canvas');c.width=128;c.height=96;const x=c.getContext('2d');x.drawImage(v,0,0,128,96);r.left=[...x.getImageData(24,48,1,1).data];r.right=[...x.getImageData(104,48,1,1).data];const q=v.getVideoPlaybackQuality();r.quality={total:q.totalVideoFrames,dropped:q.droppedVideoFrames};r.time=v.currentTime;
        r.pass=r.left[0]>230&&r.left[1]<20&&r.left[2]<20&&r.right[0]<20&&r.right[1]<20&&r.right[2]>230;
      }catch(e){r.pass=false;r.error=String(e);r.mediaError=v.error?{code:v.error.code,message:v.error.message}:null;}finally{v.pause();}results.push(r);
    }return results;
  });
  await delay(2500); // Media property batches are asynchronous, including the last codec.
  evidence.mediaInternals=await icdp('Runtime.evaluate',{returnByValue:true,expression:`Array.from(document.querySelectorAll('#player-list .tree-item-header')).map(el=>{el.click();return {title:el.innerText,properties:Array.from(document.querySelectorAll('#player-property-table tbody tr')).map(r=>r.innerText),log:document.querySelector('#log').innerText};})`});
  const players=new Map();
  for(const event of evidence.mediaEvents){const p=event.params;if(!players.has(p.playerId))players.set(p.playerId,{});for(const v of p.properties||[])players.get(p.playerId)[v.name]=v.value;}
  evidence.decoderSelection=[...players.values()].filter(p=>p.kVideoDecoderName).map(p=>({decoder:p.kVideoDecoderName,platform:p.kIsPlatformVideoDecoder,tracks:p.kVideoTracks,hardwareAccelerated:null,note:'Platform decoder does not prove hardware; HEVC permits VideoToolbox software fallback.'}));
  const processes=await browser('SystemInfo.getProcessInfo');const gpu=processes.processInfo.find(p=>p.type==='GPU');
  if(gpu){try{evidence.mapping=execFileSync('/usr/bin/vmmap',[String(gpu.id)],{encoding:'utf8',maxBuffer:20e6,stdio:['ignore','pipe','pipe']}).split('\n').filter(l=>/__TEXT/.test(l)&&/NVMTLDriver|libvulkan_nouveau|libnvdec_/.test(l));}catch(e){evidence.mappingError=String(e);}}
  evidence.mappedHashes={};for(const line of evidence.mapping||[]){const path=line.match(/\s(\/.*)$/)?.[1];if(path){try{evidence.mappedHashes[path]=createHash('sha256').update(fs.readFileSync(path)).digest('hex');}catch(e){evidence.errors.push('Cannot hash mapped image '+path+': '+e);}}}
  evidence.requestedICDObserved=evidence.requestedICD?Object.keys(evidence.mappedHashes).includes(evidence.requestedICD):null;
  evidence.mediaMetadataComplete=['h264','hevc'].every(codec=>evidence.decoderSelection.some(p=>p.tracks?.includes('"codec":"'+codec+'"')))&&evidence.mediaInternals.result?.value?.filter(p=>p.properties.some(v=>v.startsWith('kVideoDecoderName'))&&p.properties.some(v=>v.startsWith('kIsPlatformVideoDecoder'))).length>=2;
  evidence.pass=[...evidence.tests.webgl,evidence.tests.webgpu,evidence.tests.elements,...evidence.tests.video].every(t=>t.pass)&&evidence.mediaMetadataComplete&&evidence.screen.visibility==='visible'&&(!evidence.requestedICD||evidence.requestedICDObserved);
  await browser('Browser.close');
}catch(e){evidence.errors.push(String(e));evidence.pass=false;}
finally{for(const ws of sockets)ws.close();chrome.kill('SIGTERM');server.close();fs.closeSync(log);fs.writeFileSync(output,JSON.stringify(evidence,null,2));}
console.log(JSON.stringify({pass:evidence.pass,tests:evidence.tests,decoderSelection:evidence.decoderSelection,requestedICDObserved:evidence.requestedICDObserved,errors:evidence.errors},null,2));
process.exitCode=evidence.pass?0:1;
'''


def functional_browser():
    import argparse
    import os
    import signal
    parser = argparse.ArgumentParser(description='Disposable-profile Chrome pixel/compute/media tests (not a hardware-decoding certification).')
    parser.add_argument('--functional', action='store_true', required=True)
    parser.add_argument('--chrome', required=True, help='Chrome executable; never a user profile')
    parser.add_argument('--media-dir', required=True, help='directory containing red-left/blue-right h264.mp4 and hevc.mp4')
    parser.add_argument('--output', required=True, help='JSON output path; Chrome stderr is saved alongside it')
    args = parser.parse_args()
    import shutil
    if not shutil.which('node'):
        parser.error('Node >=22 with built-in WebSocket is required')
    if not pathlib.Path(args.chrome).is_file() or not os.access(args.chrome, os.X_OK):
        parser.error('--chrome must name an executable file')
    for name in ('h264.mp4', 'hevc.mp4'):
        if not (pathlib.Path(args.media_dir) / name).is_file():
            parser.error('missing fixture: ' + name)
    output = pathlib.Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='nvmtl-chrome-') as profile:
        process = subprocess.Popen(['node', '--input-type=module', '-e', CHROME_FUNCTIONAL_JS,
                                    str(pathlib.Path(args.chrome).resolve()), str(pathlib.Path(args.media_dir).resolve()),
                                    profile, str(output)], start_new_session=True)
        try:
            return process.wait(timeout=180)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return 1


import sys
if '--functional' in sys.argv:
    sys.exit(functional_browser())

ROOT = pathlib.Path(__file__).resolve().parents[1]
source = (ROOT / "plugin/NVMTLDevice.m").read_text()

start = source.index("static bool nvmtl_angle_chrome_family(void)")
end = source.index("static bool nvmtl_process_is_allowed(void)")
region = source[start:end].replace("__attribute__((constructor)) ", "")

fixture = r"""
#include <climits>
#include <initializer_list>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <unistd.h>

#define LOG_NOTICE 5
// the app path the bundle check reads; "" = a process outside any .app
static const char *g_path = "";
int proc_pidpath(int, void *buf, unsigned size) {
    if (!g_path[0]) return 0;
    strncpy((char *)buf, g_path, size - 1); ((char *)buf)[size - 1] = 0;
    return (int)strlen((char *)buf);
}
static const char *g_name = "";
static const char *g_opt = nullptr;
static char g_angle[4096] = {0};
static int g_logs = 0;

const char *getprogname(void) { return g_name; }
char *getenv(const char *k) {
    if (!strcmp(k, "NVMTL_ANGLE_METAL")) return (char *)g_opt;
    if (!strcmp(k, "ANGLE_FEATURE_OVERRIDES_DISABLED")) return g_angle[0] ? g_angle : nullptr;
    return nullptr;
}
int setenv(const char *k, const char *v, int) {
    if (!strcmp(k, "ANGLE_FEATURE_OVERRIDES_DISABLED")) {
        strncpy(g_angle, v, sizeof g_angle - 1);
        g_angle[sizeof g_angle - 1] = 0;
    }
    return 0;
}
static void syslog(int, const char *, ...) { g_logs++; }
// the images dyld reports as loaded; empty = none of ANGLE's
static const char *g_images[4] = {nullptr};
uint32_t _dyld_image_count(void) { uint32_t n = 0; while (n < 4 && g_images[n]) n++; return n; }
const char *_dyld_get_image_name(uint32_t i) { return i < 4 ? g_images[i] : nullptr; }

""" + region + r"""

static void run(const char *name, const char *opt, const char *pre, const char *want, const char *path = "") {
    g_name = name;
    g_path = path;
    g_opt = opt;
    if (pre) {
        strncpy(g_angle, pre, sizeof g_angle - 1);
        g_angle[sizeof g_angle - 1] = 0;
    } else {
        g_angle[0] = 0;
    }
    g_logs = 0;
    nvmtl_angle_allow_nvidia();
    if (strcmp(g_angle, want) != 0) {
        fprintf(stderr, "%s opt=%s pre=%s -> '%s', want '%s'\n",
                name, opt ? opt : "-", pre ? pre : "-", g_angle, want);
        exit(1);
    }
}

static const char *g_app_exe = "", *g_plain_exe = "";
int main(int argc, char **argv) {
    if (argc > 2) { g_app_exe = argv[1]; g_plain_exe = argv[2]; }
    // Chrome and Chromium, including their helper processes, are on by default.
    run("Google Chrome", nullptr, nullptr, "disableMetalOnNvidia");
    run("Google Chrome Helper (Aperitif)", nullptr, nullptr, "disableMetalOnNvidia");
    run("Chromium", nullptr, nullptr, "disableMetalOnNvidia");
    run("Chromium Helper (GPU)", nullptr, nullptr, "disableMetalOnNvidia");
    // WebKit's separate GPU process uses ANGLE Metal too; do not match Safari/WebContent indiscriminately.
    run("com.apple.WebKit.GPU", nullptr, nullptr, "disableMetalOnNvidia");
    run("com.apple.WebKit.GPU", "0", "other", "other");
    run("com.apple.WebKit.GPU", nullptr, "other", "other:disableMetalOnNvidia");
    run("com.apple.WebKit.WebContent", nullptr, nullptr, "");
    run("com.apple.WebKit.GPU.fake", nullptr, nullptr, "");
    // Everything else stays opt-in.
    run("Safari", nullptr, nullptr, "");
    run("Slack Helper (Renderer)", nullptr, nullptr, "");
    run("QQ Helper", nullptr, nullptr, "");
    // PR #404: Edge, Brave, Electron apps and Discord are on by default too.
    run("Microsoft Edge Helper (GPU)", nullptr, nullptr, "disableMetalOnNvidia");
    run("Brave Browser Helper", nullptr, nullptr, "disableMetalOnNvidia");
    run("Discord Helper (Renderer)", nullptr, nullptr, "disableMetalOnNvidia");
    // ...and any app whose bundle carries ANGLE or a Chromium framework (path given by the test on argv[1])
    run("Slack Helper (Renderer)", nullptr, nullptr, "disableMetalOnNvidia", g_app_exe);
    run("Slack Helper (Renderer)", nullptr, nullptr, "", g_plain_exe);
    // ...and any process that has ANGLE's own libraries loaded, whatever its name or bundle
    g_images[0] = "/usr/lib/libSystem.B.dylib";
    run("Vivaldi Helper (GPU)", nullptr, nullptr, "");
    g_images[1] = "/Applications/Vivaldi.app/Contents/Frameworks/Vivaldi Framework.framework/Libraries/libGLESv2.dylib";
    run("Vivaldi Helper (GPU)", nullptr, nullptr, "disableMetalOnNvidia");
    run("Vivaldi Helper (GPU)", "0", nullptr, "");
    g_images[1] = nullptr;
    // The environment variable still overrides both ways.
    run("Safari", "1", nullptr, "disableMetalOnNvidia");
    run("Google Chrome", "0", nullptr, "");
    // An existing ANGLE override is kept, without duplicating the rule.
    run("Google Chrome", nullptr, "foo", "foo:disableMetalOnNvidia");
    run("Google Chrome", nullptr, "disableMetalOnNvidia", "disableMetalOnNvidia");
    // Preserve long caller overrides and append the complete rule, including across the old 1024-byte limit.
    for (size_t n : {1000u, 1023u, 1100u}) {
        char pre[1200], want[1300];
        memset(pre, 'x', n); pre[n] = 0;
        snprintf(want, sizeof want, "%s:disableMetalOnNvidia", pre);
        run("Google Chrome", nullptr, pre, want);
    }
    printf("ok\n");
    return 0;
}
"""

with tempfile.TemporaryDirectory() as tmp:
    exe = pathlib.Path(tmp) / "test_angle"
    subprocess.run(
        ["xcrun", "clang++", "-std=c++17", "-O0", "-x", "c++", "-o", str(exe), "-"],
        input=fixture.encode(), check=True,
    )
    app = pathlib.Path(tmp) / "Slack.app"
    (app / "Contents/Frameworks/Electron Framework.framework").mkdir(parents=True)
    (app / "Contents/MacOS").mkdir(parents=True)
    result = subprocess.run([str(exe), str(app / "Contents/MacOS/Slack Helper"), str(pathlib.Path(tmp) / "plain/bin/tool")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok", result.stdout

print("ok: ANGLE Chromium-family/WebKit GPU policy, exact process matching, opt-out and preserved overrides")

# Exercise the real browser inspector: a denied vmmap is UNKNOWN; decoder mapping is not rendering proof.
with tempfile.TemporaryDirectory() as directory:
    tools = pathlib.Path(directory)
    stubs = {
        'pgrep': '#!/bin/sh\n[ "${PROBE_PGREP_ERROR:-0}" = 1 ] && exit 2\ncase "$*" in *com.apple.WebKit.GPU*) printf "101\\n102\\n103\\n104\\n";; *Google\\ Chrome\\ Helper*) printf "%s\\n" "Google Chrome Helper (Aperitif) --type=gpu-process" | /usr/bin/grep -Eq "$2" && printf "105\\n";; *) exit 1;; esac\n',
        'vmmap': '#!/bin/sh\ncase "$1" in 101) echo "permission denied" >&2; exit 1;; 102) echo "/Library/GPUBundles/NVMTLDriver.bundle/Contents/MacOS/NVMTLDriver";; 103) echo "/Library/GPUBundles/nvmtl/libnvdec_h264_t2d.dylib";; 104) echo "/System/Library/Frameworks/Metal.framework/Metal";; 105) echo "/Library/GPUBundles/NVMTLDriver.bundle/Contents/MacOS/NVMTLDriver";; esac\n',
        'xcrun': '#!/bin/sh\nexit 1\n',
        'log': '#!/bin/sh\nexit 0\n',
    }
    for name, content in stubs.items():
        target = tools / name
        target.write_text(content)
        target.chmod(0o755)
    import os
    env = dict(os.environ, PATH=str(tools) + ':/usr/bin:/bin:/usr/sbin:/sbin')
    result = subprocess.run(['bash', str(ROOT / 'tools/web_accel_check.sh'), '--processes-only'],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    for pid, expected in [('101', 'UNKNOWN'), ('102', 'PLUGIN MAPPED'), ('103', 'CODEC ONLY'), ('104', 'NOT OBSERVED')]:
        line = next(line for line in result.stdout.splitlines() if ('pid ' + pid) in line)
        assert expected in line, (pid, expected, line)
    assert 'NOT on NVIDIA' not in result.stdout
    chrome = next(line for line in result.stdout.splitlines() if 'Chrome GPU' in line)
    assert 'pid 105' in chrome and 'PLUGIN MAPPED' in chrome, chrome
    failed = subprocess.run(['bash', str(ROOT / 'tools/web_accel_check.sh'), '--processes-only'],
                            env=dict(env, PROBE_PGREP_ERROR='1'), capture_output=True, text=True, timeout=30)
    assert failed.returncode == 0 and 'UNKNOWN (process enumeration failed:' in failed.stdout
    assert 'not running' not in failed.stdout
print('ok: browser inspector preserves unknown/codec-only evidence instead of claiming rendering acceleration')
