# NullMoth 2.8.1 + local patches: install guide

## Current package — 2026-11-10 (post-reboot verified; qualification limits apply)

`dist/nullmoth-nvidia-2.8.1-local-patched.tar.gz` is updated in place on top of the previous local-patched NullMoth 2.8.1 package. No new driver, bundle or payload file. Only the existing Metal plugin binary, NVAccel binaries and NVK dylib (plus inventory/provenance/checksums) differ from the original 2.8.1 package; other kexts, firmware, translator, configuration and install/uninstall scripts are unchanged.

- Installed/tested Metal plugin: `480dfb209e36a2cc79cbe0bf648f196c10325ff865250650defa2d9e0d5ebc02` (installed baseline `29757a3d`, earlier `343c47dd`, `9538108f`); NVAccel rebuilt for macOS 15 and 26 with the PR #467 port (default off); the 9 NVDEC/NVENC codec dylibs are byte-patched (two instructions each) and re-signed.
- NVK: `27cb31945d6366b0585537a9ad61bf4db2b6ca93832e7b7374586a4717404342` (Mesa 17ca6174, blocking GPU wait, qualified AUTO binding).
- Archive SHA256: see `nullmoth-nvidia-2.8.1-local-patched.tar.gz.sha256`.

## Post-reboot verification and release boundary — 2026-11-10

**Installed files and WindowServer's loaded image verified.** Plugin SHA-256 `480dfb20…` and loaded UUID `7301882A-EA42-3FAC-B3F5-669001684565` match this build; all five loaded NullMoth kext binaries match their packaged versions. Bundle signature verified. No reinstall is needed for the documentation/diagnostic update in this pass.

- Installed-driver Metal checks: **16 passed**, one unavailable-source skip; GPU storage-boundary readback: **96 pixels verified** at slots 62, 63 and 127. Full Python sweep: **92 passed** (with required translator, Rust dependencies and historical negative controls).
- Post-reboot targeted workload, four paired baseline/installed comparisons: CPU **0.725 → 0.627 ms/frame (-13.5%)**. Normal workload CPU 0.707 → 0.714 ms/frame: no demonstrated improvement. These are CPU encoding measurements, not desktop FPS or input latency.
- Eight live compositor windows: active mode 164.875 Hz; software callbacks **152.0–164.6/s**, p95 **7.69–12.43 ms**; CPU/output update **4.57–11.83 ms**. No late/dropped/failed transactions reported in these windows; **three flip-latch timeout increments** occurred. Changing workload/boot warm-up means these runs do not prove a global WindowServer speedup.
- macOS recorded a **CPU-resource diagnostic**, not a WindowServer crash; its heavy stack is first-login shader translation/pipeline creation. No matching crash report was found, but absence of a report is not stability certification.
- The original CRC collector itself correlated with **111 additional latch timeouts** in a short control, versus zero in the immediately following ordinary measurement windows. It is an intrusive diagnostic, not a neutral latency probe. It now reports counter changes and aborts learning on the first added timeout; the live retest correctly exits **2 / INCONCLUSIVE**. Initial sampled CRCs had zero unknown greys, but do not certify unsampled refreshes or physical flicker/tearing. Three surface-GPU timeout increments were also observed across the broader stress interval; do not report a timeout-free session.

**Production sign-off is NOT granted.** The package is built, installable, rollback-documented and locally tested, but residual timeouts, workload-sensitive latency, long-session stability and physical presentation remain qualification limits. Do not publish this as a guaranteed production/flicker-free release. No GPU safety waits or security settings were weakened to make tests pass.

The source/test/docs changes and small patch archive are committed to the fork. The 241 MB prebuilt package remains a local deliverable; no GitHub Release was published. Source builds need the dependencies described in README and the patch archive's APPLY.md.

## Pre-install descriptor/stability candidate notes — 2026-11-10

**Historical pre-install status:** built and isolated-GPU-tested; subsequent installed verification is above. This replaces only the existing plugin in the existing locally patched NullMoth 2.8.1 package. NVK, kexts, codecs, configuration and install scripts are unchanged in this step. The prior installed baseline was `29757a3d`; the tested update is `480dfb20`.

- **Performance:** binding a texture/sampler that the current shader does not read forced a fresh descriptor-set allocation. The new path reuses the last immutable set when all actually read descriptors still match. Six alternating pairs of 1,600 frames × 120 draws: targeted workload **0.791 → 0.654 ms CPU/frame (17.3% lower)** and **0.670 → 0.560 ms wall/frame (16.3% lower)**. Normal workload 0.792 → 0.788 ms CPU/frame is noise, not a demonstrated general speedup. All 24 benchmark processes passed pixel readback. These are synthetic CPU encoding costs, not display FPS or input-to-photon latency.
- **Correctness:** 321 binding slots had only 256 change bits. High storage-texture slots could overwrite neighboring cache metadata, and copy-mask checks could read beyond a four-word mask. Change bits now cover all slots; copy masks are bounds-checked; upper storage changes cannot masquerade as buffer-only changes. This fixes a reproduced memory-safety issue, not a proven cause of a particular visible flicker.
- Dirty history is retained when an unread binding is ignored, so a later pipeline that reads it cannot reuse stale state. Actual changed image views/samplers, storage writes, uniform/texel layouts, page rollover and recycled descriptors still invalidate reuse. Completion fences, hazard collection, resource retention, refresh settings and presentation pacing are not weakened.
- Checks: source-extracted ASan/UBSan regressions; actual GPU A-B-A writes/readback at Metal storage slots 62, 63 and 127 (96 pixels); 11 candidate surface-order cases; mixed synchronous/asynchronous pipeline stress and 512 specialized compute values, zero wrong. Full Python sweep: **92/92 passed**; detailed candidate validation and source/binary hashes are recorded in the package identity.

For a future reinstall: `sudo bash dist/pkgroot-install/pkgroot/install.sh`, then reboot so WindowServer loads the installed files. Compare `sudo bash tools/ws_present_ab.sh 1` before/after using the same displays, app load and power state. Actual 165 unique physical presents/s, input-to-photon latency and absence of tearing/flicker remain unverified.

**Rollback:** the exact known-good binary archive was retained at `.empryo/tmp/tabs/main/before-descriptor-patches.tar.gz` (SHA-256 `35893fc28af50e1241e3859bd12052902e4914a67f627b576717498eb6be85d0`). Extract it into an empty directory, run its `pkgroot/install.sh` with sudo, then reboot. The installer also preserves its normal `/Library/NullMoth/backup-*` recovery state. Do not manually mix old/new plugin files.

## Previous Chrome A/V verification — 2026-11-10

Before staging the descriptor candidate, the installed bundle matched the previous package exactly (plugin `29757a3d`), including both verified H.264 codec dylibs. **That A/V-only verification pass required no driver installation or reboot.** No new runtime optimization is retained: an experimental H.264 binary-body rewrite was rejected and restored because pixel correctness does not establish stack-unwind safety. The existing sandbox fix is preserved.

Chrome 157: H.264 and HEVC NVDEC activity confirmed inside the GPU process; AAC audio decoded and unmuted; steady playback, pause/resume, seek and 1.25× playback exercised. Median software video/media-clock offsets at normal speed ranged from -10.2 to -2.0 ms across codecs/phases. rAF p95 was 6.7 ms. Each 12 s steady window reported one dropped video frame; pause/resume/seek windows reported zero. This is **not physical speaker/display lip-sync proof** and does not establish VP9/AV1, WebRTC, DRM or HDR acceleration.

`python3 tools/bench_chrome_av_sync.py /path/to/av264.mp4 --transitions --out /tmp/av-report`

Full results, the rejected optimization, safety boundaries and next targets are in `docs/PERF-WORK.md`. Current regression results: 92 Python scripts passed; Metal 16 passed, one unavailable-source skip. The A/V-only pass left the binary archive unchanged; the later descriptor candidate above supersedes that archive.

## 2026-11-10 third post-reboot update (historical staging notes)

Verified after the reboot: plugin `343c47dd` and NVAccel match the package, kexts 2.8.1, 16/16 Metal self-test, 90/90 test scripts. WindowServer at 165 Hz: **3.86–3.94 ms CPU/update** (was 4.69–5.24, originally 6.67), callback p95 9.74–9.76 ms, flip call 182 µs, zero late/dropped/failed. `debug.nvaccel_pageoff_pending_locked` = 0 after normal use, so the PR #467 locked wait does not occur here; it stays off. Chrome 157 WebGL2 + 60 animated CSS layers: rAF p50 6.10 ms, p95 6.6–6.8 ms; the driver's share of Chrome's GPU-process CPU is small (its completion thread sleeps in `usleep`, not spins).

17. **App start: translator identity without hashing a 15 MB file.** Every process that made its first Metal library SHA-256'd `libnvmtl_translate.dylib` (48 of 55 ms). The shader-cache tag now hashes the translator's mapped code-signature blob (which holds a SHA-256 of every page), bounds-checked, with the file hash as fallback for unsigned images. `bench_metal_load` (fresh processes, 5 interleaved pairs, pixel-verified): library 56–67 → 3–5 ms, **first render 179–196 → 112–139 ms**. Cache entries are renamed `cs256-…` once (the old ones are swept).
18. **First pipeline: pool blocks cleared as handed out.** A new 32 MiB mapped buffer-pool block was memset whole (17 ms of first-touch faults inside the first pipeline). Each range, plus its guard, is now cleared before a buffer is placed on it; unmapped blocks and `NVMTL_HEAP_ZERO=0` keep the old path. `ensure_descriptors_locked` 40 → 26 ms.

## 2026-11-10 second post-reboot update (staged, needs install + reboot)

Verified after the reboot: installed plugin `9538108f` matches the package, all NullMoth kexts loaded at 2.8.1, `NVMTL_VRAMC_N 128`, `debug.nvaccel_iop_copies` present (0 CPU copies), zero flip failures. WindowServer with the 165 Hz workload: **4.69–5.24 ms CPU per output update vs 6.67 ms before** (128-slot cache + earlier fixes), callback p95 10.4–11.2 ms vs 13.9 ms, zero late/dropped/failed. The ungated flip (default) keeps the flip call at ~187 µs vs ~6.7 ms gated and 127 vs 83 head-0 transactions/s.

14. **Sampler creation in WindowServer 12–18x faster:** CoreDisplay creates a pixel-coordinate sampler on every display-pipe commit; 62 of the 75 samples it cost were `-[NSDictionary description]` for a log line a release plugin never writes. Now formatted only when the process opted into its log. 24–26 µs → 1.4–2.0 µs per sampler.
15. **Pixel-sampler key no longer re-serialized per encoder:** encoders are per frame, so every frame's first draw ran `NSJSONSerialization` on the same sampler sets. A bounded (32), locked process-wide list now remembers them. `BENCH_PIXEL_SAMPLER=1 tools/bench_ca_draws` (pixel-checked): 0.31–0.33 → 0.22–0.25 ms CPU/frame, 5/5 interleaved wins.
16. **Upstream PR #467 (unlocked page-off wait), ported default OFF:** `NVResource::pageoff` can wait up to 100 ms for WindowServer's GPU while holding the accelerator lock, stalling every RM call. Ported with the owner check and resource re-read; off by default because the family caller's reliance on the lock across pageoff is unproven here. New counter `debug.nvaccel_pageoff_pending_locked` shows how often that locked wait happens; try it live with `sudo sysctl -w debug.nvaccel_pageoff_unlocked_wait=1` (back with `=0`, no reboot).

Not adopted: #476/#464 deferred IOSurface uploads (1,700 lines, off by default upstream; measured here the CPU-drawn window upload is PCIe-bandwidth bound: ~0.7 ms per 1280x800 surface whether imported+waited or memcpy+async). Kernel/video PRs as in the previous review.

## 2026-11-10 post-reboot update (installed live)

8. **Chrome hardware video decode fixed:** the codec dylibs opened their `/dev/null` connection token `O_RDWR`; Chrome's GPU sandbox allows only reads of `/dev/null`, so every in-process NVDEC open failed silently (`nvd264: open /dev/nvidiactl failed`) and VideoToolbox fell back to Apple's software decoder (`AppleH264SW` in VTDecoderXPCService). `tools/nvcodec_devnull_fix.py` rewrites exactly the two `mov esi, O_RDWR|O_CLOEXEC` sites in `_nvrm_xnu_open` to `O_RDONLY|O_CLOEXEC` (as NVK already does) and re-signs. Verified in Chrome 157: H.264 and HEVC decode in the GPU process on NVDEC (`nvd264_detile`, `nvd265_detile_rows`), no software decoder, no open failures. Also applies to NVENC (WebRTC encode).
9. **VideoToolbox / Photos / Preview scaling 6–14x faster:** `nvmtl_needs_visible_link` scanned the whole AIR text on every specialized function (80% of a VTPixelTransfer); now remembered per AIR text. The AIR SHA-256 key, grid-only scan and reflection JSON parse are also cached per immutable AIR/reflection object; the pipeline key log no longer hashes AIR unless logging is enabled; static samplers no longer call `MTLCreateSystemDefaultDevice` (a WindowServer round trip) per dispatch. 1920x1080→960x540: 168 → 11 ms; 4032x3024→1024x768: 189 → 37 ms; output fingerprints identical.

10. **Cache safety:** the visible-link and grid-only caches only remember answers for AIR text known to be immutable (marked by `nvmtl_air_to_text`); other strings are scanned each time. The cached static-sampler device falls back to a fresh lookup if the first one returned nil.

11. **Window-memory setup thrash (compositor):** the per-surface VRAM import cache had 48 slots. With more live surfaces than that (windows, layer backing stores, Chrome tiles) the oldest import was destroyed and the same surface imported again on WindowServer's render thread. Measured with `BSW_SURFACES=n tools/bench_surface_wrap_cost` (pixel-checked): 48 slots 10 µs/wrap at 40 surfaces but 147–179 µs at 80–120; 128 slots 15–20 µs at 40–120 (noisy machine, load ~6). Above 128 live surfaces it still thrashes (194–244 µs at 200). Imports still expire after 1 s idle. WindowServer itself picks this up only after a reboot/logout.

12. **Upstream PR #481 (Chrome/Edge flicker):** Chromium-family processes (Chrome, Chromium, Edge, Brave, Vivaldi) report a command buffer "scheduled" only after their IOSurface writes are published, so the consumer process never composites a half-published layer. Other processes keep policy 3; `NVMTL_SCHEDULE_AFTER_SURFACE_WRITES=0..3` still overrides. Interleaved Chrome 157 runs showed no rAF or video-drop cost.
13. **Upstream PR #530 (dual-GPU crash):** constexpr samplers are created on the shader's own device instead of `MTLCreateSystemDefaultDevice()`, which can be an AMD iGPU (NootedRed) and crashed in `nvk_UpdateDescriptorSets`; also removes a WindowServer round trip.

Chrome 157 with this build: WebGL1/2 pixel readback and WebGPU adapter pass on ANGLE Metal; one playing video (H.264 or HEVC, NVDEC) keeps rAF at 164 Hz (p50 6.10 ms, p95 6.70 ms). Two simultaneous playing videos drop Chrome's rAF to 41 Hz (every 4th vblank) **even with `--disable-gpu` and software decode**, so that is Chrome's own pacing, not this driver.

## Source patches

`dist/nullmoth-2.8.1-local-patches.tar.gz` holds `local-patches.diff` (every source change against upstream tag v2.8.1, commit 41a6494), the list of changed/added files, and `APPLY.md`. Apply with `git apply local-patches.diff` in a v2.8.1 checkout.

## What changed versus the installed build

1. **Metal device initialization no longer regresses:** NVK placeholder binds are rewritten only for the exact qualified raw artifact (`ec60a89d…`); unknown builds keep raw binds with a warning. All 2,940 affected slots resolve identically; no collisions across 343 loaded images.
2. **Async APIs no longer block the caller:** `newLibraryWithSource:…completionHandler:`, specialized-function and mesh async wrappers run on the existing bounded worker pool with snapshotted inputs. Parallel requests for one AIR blob share a single decode.
3. **Less per-frame allocation:** IOSurface publication/busy keys for the two hot planes are cached instead of formatted per frame.
4. **Spinning GPU wait fixed in NVK:** waits block instead of spinning; compute/image CPU per wait matches polling within noise, with identical results.
5. **Builder safety:** NVK output is staged, verified and published atomically; deployment target pinned to 15.5.
6. **High refresh:** the VBL period now accepts real high-refresh modes (540/600 Hz regression cases pass) instead of falling back to a 60 Hz period.
7. **Chrome:** inspector recognises Chrome 157's `Google Chrome Helper (Aperitif)` GPU process.

## Measured (RTX 4060 Ti, macOS 26.7.2, fresh-process medians)

| Metric | Installed | This package |
|---|---:|---:|
| Metal device init | 48.9 ms | 51.4 ms (unchanged, noise) |
| Async library call blocks caller | 58.0 ms | 0.72 ms |
| Async specialized function, caller blocked | 19.9 ms | 0.12 ms |
| First verified render | 183.7 ms | 188.3 ms (noise) |

Desktop on the installed stack: VG248 at 164.875 Hz, display-paced callbacks p50 6.06 ms; Chrome 157 WebGL1/2 and WebGPU pass on ANGLE Metal, rAF p50 6.0 ms / p95 6.8 ms. These are callback timings, not physical scanout; this is not a locked-165 FPS guarantee.

## Verification

- 89/89 Python regression scripts pass (new: `test_nvcodec_devnull_fix.py`; `test_security_hardening.py` updated for the cached visible-link query); metal selftest 16/16; GL translation passes.
- Async stress: 12 concurrent libraries, 384 verified GPU values, error callbacks; 33 surface-order cases × 100 frames in reuse modes 0/1/2; 36 serial + 36 async pipelines; zero wrong.
- Strict signatures, every payload checksum, inventory and the 26.7.2 platform gate pass; installer equals `package/install.sh`.
- Live counters: zero accelerator failures; framebuffer latch timeouts are rare (1,683 over 5.5 h / 1.26 M flips) and not increasing at idle.

## Known limits (not fixed)

- Chrome hardware video decode inside Chrome's GPU sandbox is not proven; Chrome uses its own decoder path. VideoToolbox hardware decode works outside the sandbox.
- `NVMTL_SNAPSHOT_REUSE` stays default 0 (mode 2 is opt-in: −4% CPU median, inconsistent).
- The new `debug.nvaccel_iop_copies` diagnostic counter is in source only; kexts were intentionally not rebuilt to avoid an auxiliary kernel collection change.
- Safari is out of scope. macOS 15 is metadata-compatible but not physically qualified.

## Install

Have a working OpenCore USB/recovery path and the **1401: Remove NVIDIA driver** boot entry, and back up `EFI/OC/config.plist`. Local test build, not an official release.

```bash
cd ~/Desktop/Code/nvidia-macos-driver/dist
shasum -a 256 -c nullmoth-nvidia-2.8.1-local-patched.tar.gz.sha256
(cd pkgroot-install/pkgroot && shasum -a 256 --strict -c SHA256SUMS --quiet)
sudo bash pkgroot-install/pkgroot/install.sh
sudo reboot
```

On another machine: extract the tarball into an empty directory, verify as above, run `sudo bash pkgroot/install.sh`, reboot.

After reboot:

```bash
shasum -a 256 /Library/GPUBundles/NVMTLDriver.bundle/Contents/MacOS/NVMTLDriver /Library/GPUBundles/nvmtl/libvulkan_nouveau.dylib
bash tools/metal_selftest.sh
bash tools/ws_frame_cost.sh 15
bash tools/web_accel_check.sh
```

Rollback: boot the 1401 remove entry, or restore `/Library/NullMoth/backup-20261109-204916.xUN75V`.

## Rebuild

`RELEASE=1 bash build/build_plugin.sh <output-directory>` builds this checkout with Xcode and Vulkan headers (`VULKAN_HEADERS` selects the include directory).

`bash build/build263.sh` builds the patched Mesa 17ca6174 export (minimum macOS 15.5, atomic publication). Binding policy defaults to AUTO: only the exact qualified raw artifact is rewritten; `NVMTL_NVK_SELF_BIND=0` keeps raw binds, `=1` is an experimental strict-checked force. Requalify every new raw build. Nothing was committed or pushed.
