# Performance work: starting point (local, on top of NullMoth 2.8.1)

Local working notes for this tree. These are changes to the existing NullMoth sources, not a separate driver.
Measured on an RTX 4060 Ti (AD106, 10DE:2803, 8 GB BAR1), i5-13400F, macOS 26.7.2, VG248 at 164 Hz + Dell at 60 Hz.

## Upstream PR review — 2026-11-10

Read all open/closed PRs on nullmoth/nvidia-macos-driver. Adopted: #481 (publish Chromium surfaces before scheduled callbacks; extended to Brave/Vivaldi, malformed overrides ignored) and #530 (static samplers on the shader device). Already in 2.8.1 (re-applying leaves the tree identical): #442, #445, #458, #461. Not adopted: #474/#475/#476 (opt-in experiments; #474 is documented to skip real CPU updates, #475 needs the kext change #467, #476 changes WindowServer upload lifetime), #467/#473/#431/#459/#468/#479 (kext changes needing an auxiliary-KC rebuild and reboot qualification), #477/#487 (267k-line third-party codec imports), #450/#433 (515-file diffs), #524 (OpenCL score for 5090), #490 (single translator case).

Chrome 157 cadence with two playing videos is 41 Hz (vsync/4) with hardware decode, software decode, and `--disable-gpu`; one video or none is 164 Hz. Not a driver regression.

## Compositor import-cache thrash — 2026-11-10

- Found with a surface-count sweep: `NVMTL_VRAMC_N` 48 gave a cliff from ~10 µs to ~150 µs per IOSurface wrapper once more than 48 surfaces cycle (each miss = fd + VkDeviceMemory import + VA map, then a destroy on eviction). Raised to 128: 80 and 120 surfaces now 15–20 µs. Tests 89/89, metal selftest 16/16. WindowServer was still running the plugin from boot (old inode) during this session, so its frame cost was **not** re-measured; compare `sudo bash tools/ws_present_ab.sh 1` after reboot against 160.6 updates/s, 6.67 ms CPU/update, p95 13.9 ms.
- Not changed (correctness risk): caching the cross-process `NVMTLVramAt` tag (a late writer can legitimately move it back at the same seed; caching would show stale content), making CPU-drawn surface uploads asynchronous (the source pages must stay locked until the GPU copy completes), and scheduled-handler timing (`_scheduleAfterPublish` protects surface publication order).

## Post-reboot verification and fixes — 2026-11-10 (installed)

- Installed hashes after reboot matched the package (plugin `3e217b2c`, NVK `27cb3194`); kexts 2.8.1 loaded; zero accelerator failures; latch timeouts 4 after boot. Shutdown-stall reports name user processes (BlueTool, empryo-bridge, Python), not the driver.
- **Chrome hardware decode root cause:** codec dylibs open `/dev/null` O_RDWR; Chrome's GPU sandbox grants only `file-read-data` on it (`common.sb`), so `nvrm_xnu_open` returned -1 without a message and VT used AppleH264SW out of process. Fixed by `tools/nvcodec_devnull_fix.py` (exact two-site rewrite to O_RDONLY, re-signed). Chrome 157 scratch profile now decodes H.264/HEVC on NVDEC in the GPU process; VTDecoderXPCService shows no AppleH264SW samples. The user's already-running Chrome must be restarted to load it.
- **VTPixelTransfer:** sampling showed 80% in `nvmtl_needs_visible_link` (AIR text scan per specialization), then 40% in the AIR SHA-256 per dispatch. Cached per AIR text. 1080p→540p 168→11 ms, 12MP→XGA 189→37 ms, identical fingerprints, 30-run batches.
- **WindowServer at 164.875 Hz with the animation workload (installed build):** 160.6 aggregate updates/s, head0 103 handled tx/s, 6.67 ms CPU/update, callback p50 6.06 ms / p95 13.9 ms, zero late/drop/fail. Main-thread profile: ~45% idle; synchronous costs are surface VRAM imports on cache miss (~2.6%), CPU-written surface uploads that wait for the copy (~2.7%), `IOSurfaceCopyValue` tag reads (~1.5%). These were measured, not changed: each touches cross-process surface coherence or VRAM retention and needs its own qualification.

## Pre-install audit — current revision, 2026-11-10

This section supersedes earlier build hashes and package descriptions below. No live installation, browser fix or Safari work was performed. Chrome acceleration/WebGL/video/elements are reserved for the next task.

**Fixed before repacking:**

- Previous NVK artifact declared macOS 26.0 minimum although the package minimum is 15.5. Rebuilt with explicit C/C++/Rust/link deployment target 15.5; builder now rejects newer minimums/wrong architectures. Both 15.5 and 26.7.2 static preflight gates pass (not a physical macOS15 boot test).
- `newLibraryWithSource:options:completionHandler:` compiled synchronously on its caller. An extracted production-method fixture failed before the fix; compilation now uses the existing bounded pipeline worker pool and copies source/options before enqueueing. Callback/error and mutable-input regressions pass; nil handlers retain the original no-callback behavior.
- Builder previously replaced the last good NVK output before signing/fixups could fail. Same-filesystem staging and atomic publication now preserve old output and clean only owned staging files/lock on failure. Fault-injection regressions are in `tools/test_nvk_flat_bind_fix.py`.
- Generic flat weak-import to SELF rewriting changed host/dependency resolution, even for an NVK-style name. Native controls demonstrate flat=1 versus SELF=0 when a host/dependency supplies the name. The hardened parser validates bounds, trie/symtab exports and exact generated placeholders, requires explicit no-interposition acceptance, and rejects generic inputs. **SELF is disabled in the packaged build and builder default** until full dispatch-equivalence qualification. The experiment remains an explicit build-time opt-in, not a recommended launch tweak.
- Rebuilt the existing package inventory (old firmware/license paths were stale), full symbol-name inventory and checksums, including both existing loader aliases. No payload file or driver/bundle was added. Installer/uninstaller still match the existing source scripts.

**Measured launch trade-off (six balanced fresh processes/arm, async library + verified first render):** installed plugin/ICD median device 41.79 ms, caller blocked 64.78 ms, first render 191.52 ms; old plugin with new binding-preserving ICD device 322.18 ms, caller 69.00 ms, first render 460.92 ms; new plugin/new ICD device 324.54 ms, caller 0.63 ms, first render 469.33 ms. Thus the asynchronous API no longer stalls its caller, but disabling the unproven binding shortcut increases initialization by ~0.28 s. **Not an overall app-launch speedup.** These are Metal probe timings, not whole applications, cold boot or WindowServer CPU/frame. Earlier SELF-enabled candidate measurements below are historical, not the current package.

**Final verification:** 86 non-browser Python scripts pass; two browser-policy scripts explicitly deferred. Missing history negative controls were resolved by fetching genuine upstream tag `v1.15.0` and commit `39d70bcb1e82fc2ef30936902d2c69520daafcfa` into scratch bare Git metadata, passed via GIT_DIR (the source checkout remains unchanged). The resource-phi test now has its exact spirv0.4 dependency built in scratch; 12 IR cases pass. 14 parser/build tests and 20 source-extracted wait/sync cases pass.

Final plugin/NVK pair: 12 concurrent source libraries with caller mutation, 384 verified GPU values, 13 callbacks including a real compile error, zero wrong. Thirty-three 100-frame surface-order cases across reuse modes0/1/2 pass. 36 serial and36 asynchronous pipelines plus three mixed concurrent build rounds pass. Existing bounded async-pipeline throughput measured0.35 s serial versus0.07 s parallel; this behavior predates this patch and is not claimed as a new speedup. The packaged plugin itself also produced a verified first frame.

Final NVK with installed plugin: five rounds and125 compute/image samples per policy; compute references and identical image hashes pass, cross-queue and all three32 MiB transfers pass. Median CPU/elapsed(ms): polling compute0.165/0.545, event compute0.174/0.556; polling image3.019/3.231, event image3.097/3.239. No event-policy default changed. WindowServer PID198 and installed hashes602ffb00/890cd46a remain unchanged.

Current packaged plugin SHA256 `8db8bb6431687cca96432fe810c722f670ae19607cca4dacee58eb31c8865daa`; NVK `bfd4138a4fe7801ae8bf1336155ba1d129da4d391a0f7dd3b6b0989d29da5dd4`. Audit logs/results: `.empryo/tmp/tabs/main/preinstall-regressions/summary.json`, `preinstall-launch/final-comparison.json`, `preinstall-eventwait-final/comparison.json`, `preinstall-gpu/`. See `dist/INSTALL-LOCAL-PATCHED.md` before installing, especially the startup regression.

## Reboot verification — 2026-11-10

Current results below supersede the older planning notes. Changes remain on the existing 2.8.1 sources; no replacement driver or bundle is introduced.

- Loaded NVRM, NVRMFB, NVAccel and NVRMAGDC report 2.8.1 after the 2026-11-09 reboot. WindowServer PID 198 stayed unchanged. Installed plugin SHA256 starts `602ffb00`; the standalone fresh candidate starts `8a9ac6cf`. Re-signing it as the existing bundle reproduces `602ffb00` byte for byte; no live binary was replaced.
- VG248 active mode is **164.875 Hz**, rounded to 165 by AppKit and 164 by system_profiler; Dell remains 60 Hz. The old workload used a 240 Hz timer, so its app-update count was not presentation FPS. The existing workload now uses CADisplayLink and reports software callback intervals explicitly.
- Display-paced 20 s run: 162.8 callbacks/s, interval p50 6.063 ms, p95 9.251 ms, max 28.190 ms. A concurrent 15.076 s counter sample recorded 2,494 completed output updates across both heads, zero CPU copies, 66.07% of one WindowServer core, **3.994 ms CPU/aggregate output update**, no drops or accelerator-late increments. This is not a per-head physical-frame measurement or a driver before/after comparison.
- Interleaved gated/ungated runs (0,1,0,1), while dependency/build work ran: gated flip-call averages 5,510/5,572 us versus ungated 215/214 us. WindowServer CPU 102.9/101.9% versus 102.8/104.3%: **blocking decreased, CPU did not**. No new timeout/failure/accelerator-late counters in those four measurement windows. Head0 handled transactions were only 94–102/s under that load, not a locked 165 FPS result. The original ungated setting 1 was restored.
- Scanout CRC sampling: 10 s/301 reads/103 observed changes, then 15 s/451 reads/133 changes; all sampled changes matched the learned complete greys. Sampling itself was only ~30 reads/s and missed many refreshes. No anomaly was observed; this cannot certify no tearing, stale frames or panel-side flicker. The tool no longer prints an unconditional PASS. Cumulative latch-timeout counters did increase elsewhere during testing/build load (891 at first snapshot, 1,466 after the later CRC run); these must not be reported as zero for the session.
- `NVMTL_SNAPSHOT_REUSE`: fresh candidate passed 33 surface-order runs (11 cases x modes 0/1/2) and per-quad/clear pixel readback in all 96 benchmark processes. Two balanced long batches, 16 runs/arm total, 1,200 frames x 120 draws: candidate0 median CPU/wall 7.506/5.918 us per draw; mode1 7.793/6.064; mode2 6.956/5.515. Paired mode2 CPU median -4.32%, wins 10/16, range -31.4% to +15.5%. **Keep default 0**: mode2 remains opt-in; this does not establish a WindowServer improvement.
- Real Safari 26.6.2: Canvas2D readback passed; WebGL1/2 returned context-lost `37442` with zero pixels; WebGPU adapter absent. Its GPU process had no NVMTL mapping. Safari GPU compatibility is **not fixed**. No sandbox/security settings were weakened.
- Real signed Chrome 157, downloaded into scratch storage with a disposable profile: WebGL1/2 green readback passed on ANGLE Metal RTX 4060 Ti; WebGPU compute returned `[3,10,17,24]` with no validation error. An 8 s animation recorded 1,320 intervals, p50 6 ms/p95 6.8 ms/max 7 ms, none >12 ms. These are callback timings, not physical scanout. Initial browser startup was noisier; no universal smoothness claim. Edge, Brave and Firefox were not run.
- Fixed the existing browser inspector: Chrome 157 calls its GPU executable `Google Chrome Helper (Aperitif)`, so matching only `(GPU)` incorrectly said not running. GPU-role argv matching now observes its NVMTL/NVK mappings; regression covers the renamed helper. Mapping alone is still not rendering proof.
- Installed-driver Metal selftest: 16 passed, zero failed, one missing-source skip (`bench_host_copy_texture.m`). Plugin build: zero compiler errors, 330 existing nullability/protocol/deprecation warnings, signed x86_64, minimum macOS 15.0. No claim of warning-free or universally bug-free support.

Final unchanged-live-driver repeat after the builds: 160.1 callbacks/s, callback p50 6.045 ms/p95 13.643 ms/max 32.678 ms; aggregate 2,360 output updates in 15.081 s, WindowServer 87.66% of one core / 5.602 ms CPU per output update. Head0 handled 1,470 transactions and head1 891 in that window. One framebuffer latch-timeout increment, zero accelerator failures/late/drops, WindowServer PID still 198. This is **not locked 165 FPS**; normal-session load and mixed-head scheduling still affect pacing. Live NVK hash remains `890cd46a`, confirming no new build was installed.

### Rebuilt NVK wait path and package

`nvk/nvk-macos.patch` now limits fallback spinning to 20 us, then sleeps 50 us (<20 ms elapsed) or 200 us. The synchronous flush fallback shares the same helper. The existing graphics event count-before-recheck protocol, fault checks and defaults remain unchanged. Extracted old helper: median 38.863 ms CPU/40.056 ms elapsed over 100 simulated 400 us waits; fixed: 4.239/42.382 ms (five runs each, -89.1% CPU, +5.8% elapsed). Eleven boundary cases pass; this is an isolated helper experiment, not WindowServer.

Full Mesa build and repeat build succeeded: zero compiler errors/failed targets, strict codesign and RTLD_NOW load pass, only system runtime dependencies. New wait counters expose actual event-sleep, backoff-sleep and short-spin calls. Rebuilt NVK SHA256 `371ead6cc5d3d7363c153ebd43bd566dba96c4a05ad4b9445af8e1cad71518db`; 2,940 weak flat imports rebound to self. Python packages and host shader-tool dependencies were installed. Homebrew python@3.15 link conflict was avoided, not forced; the active python.org interpreter works.

Five balanced rounds of the rebuilt ICD with the installed plugin: 125 samples/workload/policy; compute and Core Image passed, image hashes identical; both event policies passed 32 cross-queue rounds and all three 32 MiB copy classes. Event calls were counted, not inferred from a release diagnostic. Poll/event median CPU (ms): compute 0.193/0.177, image 2.945/3.159; elapsed compute 0.581/0.570, image 3.232/3.409. Keep the existing app policy; event waits are not universally faster.

Separate old-vs-rebuilt NVK A/B, polling requested for both, five alternating rounds: compute CPU 0.582 to 0.174 ms (-70.1%), elapsed 0.574 to 0.556 ms; Core Image CPU 3.779 to 3.384 ms (-10.5%), elapsed 3.406 to 3.450 ms (+1.3%). Compute bytes and all image hashes agreed across builds. These compare complete NVK builds in disposable processes, not a measured WindowServer change.

Fresh plugin plus fresh ICD also passed compute, cross-queue events and transfers under both policies, and 600-frame/120-draw render readback with modes 0/1/2. Core Image additionally discovers the installed bundle; loading an explicit different-path plugin duplicates Objective-C classes, so that invalid first candidate-image run was rejected, not counted as passing. The runner uses the installed, byte-identical plugin with an explicit candidate ICD instead.

The existing local package was repacked in place with the new NVK plus provenance/checksums, preserving its kernel payload and original install/uninstall scripts. The rebuilt plugin is byte-identical after bundle signing. **The new NVK is not installed in the live session.** Installation/reboot steps and limitations are in `dist/INSTALL-LOCAL-PATCHED.md`; no current-session WindowServer CPU speedup is claimed.

## Tools

| Tool | What it answers | Needs |
|---|---|---|
| `tools/ws_frame_cost.sh [secs] [--sample]` | WindowServer CPU per completed aggregate output update; optional stack sample | sudo, a drag while it runs |
| `dist/ab-drag-test.sh` | Same counters, `debug.nvrmfb_flip_ungated` 0 vs 1 | sudo, four 10 s drags |
| `tools/bench_ca_draws.m` | Plugin CPU per draw on a CoreAnimation-shaped load, in a test process | the plugin path |
| `tools/web_accel_check.sh` | Loaded NVMTL/NVK/codec mappings only; not active rendering/decoding proof | none (sudo for some processes) |
| `tools/vt_hw_decode_check.m` | VideoToolbox's hardware-decode answer per codec, including the first-query race | none |

Plugin census (default off, in `plugin/NVMTLObjects.m`): `NVMTL_PROCESS_ENV=1 NVMTL_ENC78_CENSUS=1` for a test
process (release builds ignore environment settings without `NVMTL_PROCESS_ENV=1`), or
`sudo touch /Library/GPUBundles/nvmtl-census` and log out/in for WindowServer. It logs
`ENC78 ... setnew N setreuse N dcache N poolalloc N poolopen N setfail N` at powers-of-two encoder counts.
Remove the file to turn it off: `sudo rm /Library/GPUBundles/nvmtl-census`.

## 1. WindowServer CPU cost per frame (main cause of drag stutter)

Goal: at 164 Hz, WindowServer CPU per frame below 6.1 ms (`ws_frame_cost.sh`).

Measured 2026-11-09 (`ab-drag-test.sh`): drags reach 56-123 flips/s at 54-98% WindowServer CPU. At 98% CPU it
reached 123 of 164 frames, so the CPU, not the display path, limits it.

`bench_ca_draws` (120 draws/frame): official 2.8.1 plugin 9.4-12.1 us/draw, patched 8.1-9.6 us/draw.
The remaining per-draw profile (patched plugin):

| Leaf | Samples | Where |
|---|---|---|
| `objc_release` / `objc_retain` / `objc_msgSend` | 707 / 598 / 235 | Objective-C encoder methods in `plugin/NVMTLObjects.m` |
| `nvk_AllocateDescriptorSets` | 537 | Mesa NVK, reached from `nvmtl_snapshot_set_md` (`plugin/nvmtl_vk.c`) |
| `nvmtl_address_snapshot` | 241 | `plugin/nvmtl_vk.c` |
| `nvmtl_snapshot_set_md` | 223 | `plugin/nvmtl_vk.c` |

Census, `bench_ca_draws` 300 frames x 120 draws (2026-11-09): `setnew 30976 setreuse 30464 dcache 29952
poolalloc 1024 poolopen 0 setfail 0`. Half of the draws reuse their set; of the new sets, 97% come from the
command buffer's batch cache, with one locked pool allocation per 32 sets and no failures. The global lock is
not the bottleneck; the cost is NVK's work per new set plus the descriptor writes. That points to step 1.

Next steps, in order of payoff and risk:

1. **Descriptor-set reuse.** Every draw that changes a texture gets a brand-new set (`setnew`). Reuse a set whose
   contents match (hash of the written descriptors per command buffer) instead of allocating one. First, run the census
   in WindowServer during a drag to get the real setnew/setreuse/dcache ratio. Constraint: a set already recorded
   into a command buffer must not be rewritten until that command buffer completes.
2. **Objective-C overhead on per-draw setters.** `setFragmentTexture`/`setFragmentSamplerState`/`drawPrimitives`
   retain/release and `_cb->_resources` set inserts on every call. Candidates: skip re-adding the same object to
   `NVMTLResSet` when it is already the last one added; mark internal helpers `objc_direct`.
3. **Push descriptors** for small pipelines (`VK_KHR_push_descriptor` is already enabled at device creation for
   DLSS). Needs a separate layout without dynamic buffers; larger change.
4. **Shader prewarm** for CoreAnimation (#370): first use of each shader is translated, which causes a one-time
   hitch after each boot.

Rule for every step: behind an env/sysctl switch, default as today, A/B with `bench_ca_draws` (test process) and
then `ws_frame_cost.sh` (real WindowServer), with the existing `tools/test_*.py` passing.

## 2. Presentation, high refresh, two displays

- Patched: the flip wait can run outside the lock shared by both displays (`debug.nvrmfb_flip_ungated=1`,
  `kexts/NVRMFB/fb/nvrm-fb.cpp`). The A/B run so far had the 60 Hz display idle, so it did not test the case this
  fixes. Rerun `ab-drag-test.sh` with a video playing on the 60 Hz display.
- Patched: the software vblank timer accepts panels up to 1000 Hz.
- Open: vblank timestamp latency (#424), three scheduling hops before the stamp. Measure before changing:
  add callback-to-delivery latency counters in `hwVblIntr`/`hwVblDeliver`.

## 3. Browsers and video

`tools/web_accel_check.sh` on 2026-11-09:

| Process | On NVIDIA? |
|---|---|
| Safari `com.apple.WebKit.GPU` | **Not observed**: no NullMoth library mapped in a successful snapshot; this alone does not establish whether compositing, WebGL or video uses the GPU |
| `VTDecoderXPCService` | Yes: NVMTLDriver, NVDEC H.264 and HEVC |
| Chrome-family | ANGLE Metal rule applied (log line "NullMoth: angle") |

**Safari.** WebKit's GPU process sandbox (`/System/Library/Frameworks/WebKit.framework/Resources/com.apple.WebKit.GPUProcess.sb`)
allows reading `/Library/GPUBundles` and opening `IOAccelerator` user clients, but filters their external method
numbers to Apple's list. The plugin also talks to `NVRMUserClient`, which no rule allows. No sandbox
denial was logged, and the plugin is not mapped at all, so Metal never got as far as loading it there. Next step: find
why Metal skips the bundle in that process (log `MTLCopyAllDevices` from a process run under the GPU process's
sandbox profile). Changing Apple's sandbox is out of scope; the fix has to fit inside it.

**Video decode first-query race (reproduced; see follow-up below).** The plugin registers its NVDEC decoders while VideoToolbox answers
a process's first question, so the first `VTIsHardwareDecodeSupported` answer is wrong:

```
first query avc1 -> CPU only, later avc1 -> hardware   (same process)
first query hvc1 -> CPU only, later avc1 -> hardware
```

An app that asks once at startup and caches the answer (as some browsers do) treats H.264 as CPU-only. Next step:
register the decoders before VideoToolbox builds its answer, for example from the plugin's load path in processes
that link VideoToolbox, keeping the existing sandbox/delegation checks in `nvmtl_nvdec_register()`.

**YouTube.** VP9 and AV1 have no NVDEC registration (CPU decode). Use h264ify-type extensions until a VP9/AV1
decoder is added (PR #487 is a starting point for Ada).

## Safari and CPU follow-up patches

Initial work used source and disposable console probes only. The user subsequently authorized a backed-up Metal-plugin deployment and Safari restart for acceptance testing; see the deployment outcome below. No computer-use automation, browser settings, sandbox profiles or kernel drivers were changed. The existing tarball/diff in `dist/` do not include this follow-up; their install notes now warn about that boundary.

### WebKit vendor policy and what remains blocked

`plugin/NVMTLDevice.m` previously enabled ANGLE's `disableMetalOnNvidia` override only for Chromium-family processes. The exact `com.apple.WebKit.GPU` process now gets the same override, while `NVMTL_ANGLE_METAL=0`, existing overrides and unrelated Safari/WebContent processes retain their behavior. The production-constructor regression failed before this change and passes afterward.

Upstream WebKit's `Source/ThirdParty/ANGLE/src/libANGLE/renderer/metal/DisplayMtl.mm` enumerates Metal devices before its NVIDIA vendor gate. This patch removes that particular rejection once the plugin loads; it cannot force Metal to discover/load a plugin it has already skipped. It is **not evidence of full Safari compatibility**.

A disposable native WKWebView console probe on the currently installed stack reproduced the failure: a WebGL context was returned but `getError()` yielded `37442` (`CONTEXT_LOST_WEBGL`), vendor/renderer were null, and a cleared red pixel read back zero. A normal Metal enumeration probe did see the RTX 4060 Ti. An attempted separate GPU-sandbox-profile probe was refused by the OS, so no claim is made about executing a faithful GPU-process sandbox replica. The initial probe did not restart Safari. A later user-approved restart and deployment reproduced the same failure, as recorded below.

`tools/web_accel_check.sh --processes-only` now distinguishes **UNKNOWN** (vmmap failure/empty result), **PLUGIN MAPPED**, **CODEC ONLY**, and **NOT OBSERVED**. A permission failure used to be incorrectly reported as “NOT on NVIDIA”; a codec dylib alone also incorrectly implied rendering acceleration. Extracted fake-process tests exercise the real shell script and reproduce both distinctions. Mapped libraries are not active-rendering or active-decoding measurements.

### Decoder initialization

The H.264 and HEVC registrars now resolve the already-loaded VideoToolbox registration symbol **before** consuming `dispatch_once`. Loading Metal before VideoToolbox therefore no longer permanently suppresses a later registration attempt. Tests cover missing-VT then retry, WindowServer exclusion, Apple sandbox delegation, sandboxed Chromium behavior, explicit codec opt-outs and decode-service asynchronous registration.

For controlled unsandboxed test processes only, `NVMTL_EAGER_NVDEC=1` registers at plugin load when VT is already loaded. This experimental path is **off by default**: a VT symbol existing does not prove loader/VT lock ordering is safe during automatic discovery. It never disables a sandbox or loads VT recursively, and it does not replace the decode-service asynchronous path. In release builds, a disposable environment-driven test also needs `NVMTL_PROCESS_ENV=1`.

`tools/vt_hw_decode_check.m --plugin /path/to/NVMTLDriver` explicitly loads a candidate before the first query. In fresh test processes the baseline answered CPU-only first and hardware later for H.264/HEVC; the candidate with eager opt-in answered hardware first and later for both. VT discovery can also map the installed plugin, producing duplicate Objective-C-class warnings in this explicit-load experiment; these results demonstrate registration timing, not clean full-application rendering conformance. The public query reports capability, not actual decode-session utilization. VP9/AV1 remain CPU-only in this implementation.

### Rebuilt-plugin shader compatibility

The failed CA benchmark exposed a concrete source-build bug: `NVMTL_XLATE_LIB` in `plugin/NVMTLLibraryLoad.m` selected the same legacy `/Library/GPUBundles/nvmtl/libnvmtl_translate.dylib` path for both branches, but this machine's packaged translator is inside `NVMTLDriver.bundle/Contents/MacOS`. Both the baseline-source and candidate-source benchmark therefore failed with `newLibraryWithData: translation produced nothing`. The default lookup now prefers the actual bundle path and retains the legacy path as fallback, while preserving explicit compile-time overrides. An extracted production-macro fixture failed before the fix and checks both layouts after it. After recompilation, the previously failing candidate CA draw probe completed 300 frames × 120 draws: 1.149 CPU ms/frame, 9.57 us/draw, command-buffer status Completed. This is one synthetic run, not a WindowServer A/B speedup. This repairs rebuilt-plugin shader loading; it does not itself establish Safari-process discovery or a CPU/FPS gain.

### CPU optimization boundary

The direct-CF resource-set experiment was evaluated and removed from production: eight interleaved runs gave MRU-miss medians of 93.49 ns/add with CF versus 94.10 ns/add with the candidate default, with broad overlapping ranges (82.02–119.39 versus 86.29–118.59). This does not establish a repeatable compositor gain. Existing resource-retention behavior remains unchanged; stronger identity/MRU/seal/copy/concurrency tests and the standalone benchmark are retained. The earlier CF A/B CA draw attempts could not produce a timing comparison because both baseline and candidate failed to translate its shader with the old translator path; the separate loader fix above subsequently made the candidate run. No descriptor-set-history cache, push-descriptor rewrite or global performance tuning is shipped here. GPU utilization is not a target to maximize: the goal is correct frames within the refresh budget with less unnecessary CPU/GPU work.

### User-approved live acceptance and rollback

After explicit approval, the candidate plugin was installed with an original-bundle backup, root-owned private staging, signature verification and binary checksum checks. Only the existing Metal bundle executable/signature was changed; kernel extensions, firmware, browser preferences and sandbox rules were untouched. Safari was quit and reopened through console commands.

The **fresh Safari GPU process still did not map NVMTL**, and the native WebKit probe still returned context loss `37442` and zero pixels. Thus the primary Safari acceptance test failed; the source policy change is not a complete fix for discovery/context loss. The candidate must not be presented as a fully compatible Safari driver.

Two independent fresh console probes confirmed both `MTLCreateSystemDefaultDevice()` and `MTLCopyAllDevices()` select the same RTX 4060 Ti (registry ID `4294968703`, not low-power/removable/headless). A direct probe of the system WebKit `libANGLE-shared.dylib` reproduced the separate vendor gate: Metal EGL initialization failed with `EGL_NOT_INITIALIZED` by default and succeeded as EGL 1.5 when `ANGLE_FEATURE_OVERRIDES_DISABLED=disableMetalOnNvidia` was present before initialization. This proves that gate exists, but not why the sandboxed Safari process skips plugin loading. A passive process sample did not establish a further driver-specific cause.

Because Safari acceptance failed, the pre-test plugin bundle was restored and its strict code signature and original executable checksum were verified. Safari was restarted again; the same native WebKit probe still returned context loss `37442`, confirming the baseline failure rather than a new candidate regression. Backup location: `/Library/NullMoth/safari-plugin-backup.LVpZOL/NVMTLDriver.bundle`; the tested candidate is preserved separately in that directory. The original executable SHA-256 is `07b119783c306126bc1c632f300266542f9407987904e22f0e10120edde323d8`. Source fixes and diagnostic evidence remain available; a claim of full Safari recovery or measured WindowServer improvement would be false.

## Validation status

- All 87 `tools/test_*.py` were rerun after the follow-up changes: 87 passed, zero skipped. Required GL translation/resource-phi dependencies were rebuilt in scratch storage; authentic public Git history was used for historical negative controls. The final release-mode x86_64 plugin compiled/linked successfully. `tools/test_angle_chrome_family.py` now also tests inspector uncertainty, and `tools/test_opengl_perf.py` tests both translator package layouts. The final candidate also passed all eleven surface-order modes at 100 frames each and 16 bidirectional host/texture-copy iterations on the RTX 4060 Ti, with zero mismatches. These are not Safari GPU-process tests.
- The earlier census and ungated flip wait remain off by default. Follow-up source changes affect WebKit ANGLE policy, missing-VT registration retries and translator lookup. Eager decoder registration remains an explicit, default-off test option. A later user-approved plugin deployment/Safari restart was tested; its acceptance failure and rollback are recorded below.
- Not validated: any change's effect inside the real WindowServer over a long session. Use `ws_frame_cost.sh` before and after.

## IOSurface wrapper cost (2026-11-09, RTX 4060 Ti, macOS 26.7.2)

A 10 s `sample` of WindowServer put `-[NVMTLDevice newTextureWithDescriptor:iosurface:plane:]` at ~33 ms of CPU per second:
every Core Animation wrapper re-imported the surface's VRAM (an fd, `nvk_AllocateMemory` import, VA map) and freed it on
dealloc, and each alias image re-queried immutable format properties.

- `plugin/NVMTLDevice.m` `nvmtl_surface_vram_get/put`: wrappers of one surface and size share one import (48 entries, kept
  1 s after the last wrapper; each entry retains its IOSurface so its ID cannot be reused). `NVMTL_NO_VRAM_IMPORT_CACHE=1`
  (with `NVMTL_PROCESS_ENV=1`, or the conf file) restores one import per wrapper.
- `plugin/nvmtl_vk.c` `nvmtl_vk_fmt_props` / `nvmtl_vk_mutq_*`: format and mutable-image answers are memoized; only
  success/not-supported results are kept.
- Measured with a 3000-wrapper loop over eight 1920x1080 BGRA surfaces (GPU clear every 30th, pixel read back through a
  fresh wrapper and the CPU): 255-318 us CPU per wrapper with the cache off, 15.6-19.3 us with it on (about 15x), pixels
  correct. `tools/metal_selftest.sh`: 16 ran, 0 failed (surface-order, surface-wrappers rgba8/nv12, VT transfer).
  `tools/test_surface_publication.py` covers sharing, expiry, eviction, uncached overflow and the kill switch.
- WindowServer picks the plugin up at the next restart; compare `bash tools/ws_frame_cost.sh 15` under the same workload.

## High refresh and app start (2026-11-09, same machine)

- **Ungated landing wait now default** (`kexts/NVRMFB/fb/nvrm-fb.cpp` `gFlipUngated = 1`). Workload: a borderless 800x600
  window on the VG248 (165 Hz) redrawing and moving every frame (app ~200 frames/s), 60 Hz Dell idle-composited; two
  interleaved 10 s runs per arm via `sysctl debug.nvrmfb_flip_ungated`:

  | arm | 165 Hz head | 60 Hz head | avg flip call | late / timeouts / failures | WindowServer CPU |
  |---|---|---|---|---|---|
  | gated (old default) | 89, 92 flips/s | 60 | 6539, 6423 us | 0 / 0 / 0 | 67.8, 68.4% |
  | ungated (new default) | 136, 138 flips/s | 60 | 182, 182 us | 0 / 0 / 0 | 80.3, 81.4% |

  WindowServer CPU per output update fell from ~4.6 ms to ~4.1 ms while it produced 50% more frames.
  `sudo sysctl debug.nvrmfb_flip_ungated=0` restores the gated wait until reboot.
- **NVK flat-namespace binds** (`tools/nvk_flat_bind_fix.py`, applied in `build/build263.sh` and to the packaged
  `libvulkan_nouveau.dylib`, UUID unchanged). 2940 weak entrypoint placeholders were bound through the flat namespace;
  none resolves in any image (checked with dlsym over a Cocoa+Metal process), yet dyld walked every image's export trie
  for each: ~200 ms of `MTLCreateSystemDefaultDevice`. Rebound to the library itself (still NULL).
  `tools/bench_metal_load.m`: device creation 261-346 ms before, 43-74 ms after. `tools/metal_selftest.sh` 16/0 failed;
  `tools/test_nvk_flat_bind_fix.py` covers the rewrite.
- Remaining WindowServer cost under that workload (8 s `sample`): `commitAndWaitUntilSubmitted`/`waitUntilScheduled`
  blocking in CA's flush, per-draw `nvk_AllocateDescriptorSets` (the default-off `NVMTL_SNAPSHOT_REUSE` targets it), and a
  spinning `nvkmd_nvrm_wait_backoff` in the pump thread's fence wait (inside NVK; needs a Mesa rebuild).


## WindowServer hot spots after the 128-slot reboot (2026-11-10)

- `sample WindowServer 8` under `tools/ws_anim_workload` at 164.875 Hz: surface-in (CPU-drawn window upload) 838 samples, of which ~385 wait for the GPU copy and ~180 are the CPU copy; per-draw `nvk_AllocateDescriptorSets` 138; `newSamplerStateWithDescriptor:` 75 (62 in `NSDictionary description`); `NSJSONSerialization` under `nvmtlSamplerPipeline` 26.
- Surface-in A/B in a normal process (4 changing 1280x800 surfaces, pixel-checked): imported pages + wait 2.6 ms/frame, memcpy + async 2.5–2.8 ms/frame, encode-time blocking 2.8 ms both: bandwidth bound, so async uploads (#476) would not cut CPU; skipped.
- Fixed: the sampler description (24–26 → 1.4–2.0 µs/sampler) and the per-encoder JSON key (pixel-sampler frame 0.31–0.33 → 0.22–0.25 ms CPU). WindowServer takes effect after reboot.


## App start and third reboot (2026-11-10)

- WindowServer 165 Hz workload after the sampler fixes: 3.86/3.94 ms CPU per output update, 71–72% of one core, p95 callback 9.7 ms; the remaining driver cost is spread (per-draw descriptor set write/allocate, sampler pipeline bind, embedded bindings), no single hot spot.
- `bench_metal_load` sampled through a suspended-spawn launcher: device 48–57 ms (NVK device/channel creation 15 ms, plugin load 12 ms, IOSurface GPU policy 9 ms), library 60 ms of which 48 ms was the translator file SHA-256, pipeline 41 ms in descriptor setup of which 17 ms was the pool memset.
- After: library 3–5 ms, first render 112–139 ms vs 179–196 ms (5/5 pairs), shader cache still hits across processes (second run of a new shader 312 → 5 ms).


## Chrome A/V clocks and remaining acceleration limits — 2026-11-10

Installed and packaged plugin: `29757a3d`; code signatures and all package checksums verified. No additional runtime driver change is retained from this investigation. The known-good H.264 decoder libraries (including the sandbox `O_RDONLY /dev/null` fix) were restored byte-for-byte after the detiling experiment below. The signed package archive is unchanged.

### What was measured

`tools/bench_chrome_av_sync.py` opens its own disposable visible Chrome 157 profile, plays an unmuted 1920×1080 60-fps H.264/HEVC video with 48 kHz AAC, then tests pause/resume, seek, and 1.25× playback. It reports `requestVideoFrameCallback` media time against the media element's clock projected to Chrome's expected display time. It does **not** measure speaker arrival, panel photons, or physical lip-sync. No forced sync offsets, sandbox relaxation, or persistent Chrome flags were applied.

- H.264: active `nvd264_detile` / `nvd264_feed` stacks in Chrome's GPU process prove NVDEC activity. Steady 12 s window: 719 video callbacks, median software offset -5.65 ms, p05/p95 -11.43/-1.54 ms, one reported dropped frame out of 721. Pause/resume and seek: zero reported dropped frames, medians -1.98/-4.31 ms.
- HEVC: active `nvd265_detile_rows` / `nvd265_feed` stacks prove NVDEC activity. Steady window: 719 callbacks, median software offset -10.21 ms, p05/p95 -17.21/-6.08 ms, one reported dropped frame out of 720. Pause/resume and seek: zero reported dropped frames, medians -7.32/-5.64 ms.
- At 1.25×, both had zero reported dropped frames but 23 missed frame callbacks per 12 s. Callback gaps are **not** decoder drops or proof every physical refresh displayed a new frame.
- rAF p50/p95 across those phases: 6.1/6.7 ms on the 164.875 Hz head. Separate short audio-confirmation runs reported 10,985 decoded audio bytes per codec, unmuted, no media errors, and zero drops in their 5 s measured windows.
- GPU-process CPU during the longer runs: H.264 57.3% of one core, HEVC 29.8% (whole process, not decoder alone). Measurement windows skip the first 3 s. Earlier runs saw 8–20 drops during the first ~300 ms; startup is not drop-free.
- Whole Python regression sweep: 92/92 passed, including the new 3-test offline probe suite and GL translation with the actual dylib. Resource-phi tests received their compiled dependency directory; historical negative controls received the local upstream history. Metal self-test: 16 passed, one missing-source skip.

### Optimization investigated and rejected for shipping

H.264's shipped untiler copies one byte at a time. Sampling found 681 of 1,367 non-wait leaf samples in `nvd264_detile`. An experimental 8/16-byte-run implementation matched 48 synthetic mode/size/index/field cases byte-for-byte; helper time was 5.4–5.6 → 0.34–0.43 ms per 1080p frame and Chrome GPU-process CPU approximately halved. **It is not in the installed driver or package:** overwriting machine code without requalifying its compact-unwind/DWARF metadata is unsafe even when pixels match. Both H.264 dylibs were restored from the verified package, keeping the earlier sandbox fix. Next implementation should rebuild the decoder from source (and its unwind metadata), not overwrite the function body.

### PRs and highest-value next work

- #481 and #530 are already applied. #467 remains default off: the local locked-pageoff counter stayed zero, so enabling it has no demonstrated benefit.
- #487 adds VP9/AV1 through #477's large NVEncVT source import and fixes retained-frame ownership, but its author explicitly did **not** test Chrome. Do not equate Firefox's hardware support with Chrome integration. Existing hardware proof is H.264/HEVC only; VP9/AV1, DRM and WebRTC are not established by these tests.
- #474 reader-skip, #475 shared multi-plane surfaces and #476 deferred uploads affect cross-process ownership; no new default is enabled without real capture/readback and contention tests.
- Highest-value CPU target: source-built H.264 detiling/zero-copy output. Next GPU/compositor target: descriptor-set reuse with immutable per-draw state and bounded lifetime, benchmarked in real WindowServer (the existing experimental reuse flag stays off). Neither dropping correctness waits nor raising clocks blindly is a safe optimization.
- 60-fps video cannot become 165 unique video frames/s merely by selecting a 165 Hz display. Non-integer display cadence is distinct from audio-clock drift. Physical flash/beep capture at the actual HDMI/DP/Bluetooth output is needed before compensating audio latency; network playback/adaptive switching, hour-long drift, HDR/DRM and WebRTC remain separate acceptance tests.

Reproduce with a synchronized fixture:
`python3 tools/bench_chrome_av_sync.py /path/to/av264.mp4 --transitions --out /tmp/av-report`
The runner retains JSON, its exact HTML, the Chrome log and a GPU-process sample in the output directory. It does not touch the user's real Chrome profile or close their normal windows.


## System-wide descriptor allocation and storage bounds candidate — 2026-11-10

Scope: WindowServer/Metal CPU overhead and descriptor correctness, not browser optimization. Candidate plugin `480dfb20` is built and isolated-GPU-tested, **not installed**; installed baseline remains `29757a3d`.

### Reproduced causes and patches

- Source-extracted production `nvmtl_snapshot_set_c` test first failed: changing an unread texture slot allocated a new descriptor for every draw. Added conservative reuse only when the requested read mask and storage range are covered and unchanged, texel/uniform layouts match, the set still belongs to this recording, and any fresh immutable dynamic address table stays on the same retained page. Dirty/change evidence is deliberately retained for future readers. No GPU completion wait, barrier, retention or scheduling policy was removed.
- A second failing production-layout test demonstrated **321 slots but only 256 change bits**. Expanded `chg` to six words, audited all readers, made the buffer-only fast path check upper words too, and bounded four-word copy masks to indices below 256. High storage descriptors are rewritten rather than copied through nonexistent mask bits.
- ASan/UBSan fixtures cover every slot, no adjacent cache-state corruption, actual slot-writer invalidation on view/sampler replacement, mask narrowing/re-expansion with pending changes, texel/uniform layout changes, page rollover, retired/recycled sets and high storage writes. Candidate GPU checks at slots 62/63/127 verify A-B-A output in both textures (96 pixels); all 11 surface-order cases passed; pipeline stress produced zero wrong values.

### Measurements and limits

`BENCH_UNUSED_BINDINGS=1 tools/bench_ca_draws <plugin> 1600 120`: six interleaved old/candidate pairs, same process/device workload and default `NVMTL_SNAPSHOT_REUSE=0`. Median CPU/frame **0.7909785 → 0.654129 ms (-17.3%)**; wall/frame **0.6695595 → 0.5601815 ms (-16.3%)**. Normal alternating-used-texture workload: CPU 0.791725 → 0.788482 ms (no meaningful change). All 24 processes verified final pixels. This demonstrates the specific unread-binding case, **not** a real WindowServer speedup, higher physical FPS or reduced input-to-photon latency.

Build is x86_64, minimum macOS 15.0, zero compiler errors and 330 existing warnings. Only macOS 26.7.2/RTX 4060 Ti was exercised. Kexts, NVK, video libraries and config stay unchanged. The 128-slot surface cache, ungated display wait and previous patches remain included. No new driver or bundle identity; existing package/patch archives are updated in place. Live desktop smoothness and no-flicker/tearing qualification require installation and reboot; do not claim a guaranteed 165 unique scanouts/s from callback rates.


## Installed acceptance — 2026-11-10, RTX 4060 Ti / macOS 26.7.2

The user's reboot loaded plugin `480dfb209e36a2cc79cbe0bf648f196c10325ff865250650defa2d9e0d5ebc02`. WindowServer's sampled image UUID `7301882A-EA42-3FAC-B3F5-669001684565` matches the on-disk binary; the full bundle and all five kext binaries match the package. Signature verified. Installed Metal suite: 16 passed, one source unavailable; storage slots62/63/127:96 pixels correct. Full Python suite:92 passed.

Four paired old/installed runs (1,600 frames × 120 draws): targeted unused-slot churn CPU 0.724666 → 0.6266375 ms/frame (-13.5%), wall0.5998885 →0.51944 ms/frame; normal CPU0.7072145 →0.714101 and wall0.586538 →0.613278 (no demonstrated improvement). This qualifies the narrow optimization, not overall desktop speed.

All eight 10 s compositor windows are retained in the local acceptance summary. CPU/update:11.832,11.563,4.569,4.581,5.431,6.085,9.638,7.486 ms. Callbacks152.0–164.6/s at164.875Hz, p95 7.687–12.427 ms, maximum interval61.407 ms. Zero reported late/drop/fail transactions, three latch timeout increments. No controlled same-workload old/new WindowServer comparison was performed, so no general compositor improvement is asserted.

A boot CPU-resource diagnostic (87% of one core over103s; no action taken) attributes its heaviest stack to CoreAnimation pipeline creation and `nvmtl_translate_fc`. It is **not** a crash report. Costs vary with warm-up and background app activity.

The CRC test is intrusive: a separate3s animation plus calibration produced111 extra latch timeouts and11 locked page-offs; immediately following ordinary workload windows hadzero latch timeout increments. The kernel CRC sysctl synchronously invokes NVKMS `getCRC32`. Fixed the existing collector to track counter deltas, abort calibration when a timeout advances, and return INCONCLUSIVE rather than a clean-looking pass. Live retest stopped on1 extra timeout with exit2. Counter helpers and abort policy are regression-tested. Initial20s sampling found211 complete-grey changes/602 reads and0 unknown, but those reads cannot certify physical tearing/flicker or every165Hz refresh. Across the broader CRC/GPU-stress interval, three surface-GPU timeouts were observed; later isolated abandoned/CPU/wait surface controls passed without additional surface or flip timeouts.

**Release boundary:** installed and locally tested, not universally production-certified. Long-session stability, residual timeout causes and physical presentation remain unresolved. Keep fences, the default-off page-off experiment, and the existing rollback path; do not substitute capability flags or suppressed diagnostics for acceptance. The runtime payload is unchanged by this pass; only source diagnostics, tests, provenance/docs and archives are refreshed. No upstream PR/issue/comment or GitHub binary release is opened.
