# NullMoth NVIDIA driver on macOS — frame pacing, high refresh and RTX 4060 Ti: engineering analysis and fix

Nov 8, 2026 · @Buddkha

> **Local implementation review — Nov 9, 2026:** This is an engineering analysis, not a hardware certification. The source patches below modify the existing driver; no replacement driver is introduced. The original split-wait proposal is not safe to apply verbatim: its ungated poll can race notifier replacement and teardown. See the revised implementation and verification section below. No claim of bug-free operation or full RTX 4060 Ti validation is made.

## Executive summary

Desktop stutter, sub-refresh window dragging and uneven browser scrolling on this driver are not one bug. They come from three independent bottlenecks, each visible in the source (driver 2.8.1, commit `41a6494`) and each matching measurements already posted in the project's GitHub issues. Your dual-display setup (164 Hz ASUS VG248 + 60 Hz Dell SE2218HV) hits the first one directly.

| # | Bottleneck | Where in the code | Effect on your machine | Evidence |
| --- | --- | --- | --- | --- |
| 1 | The wait for a flip to reach the panel sleeps while holding a **single global lock shared by every display** (the "display gate") | `kexts/NVRMFB/fb/nvrm-fb.cpp`: `flipToMemory` → `submitConfig` → `waitFlipDone` | The 60 Hz Dell's wait (up to 20.8 ms) blocks the 164 Hz VG248's frames and every cursor update. Biggest single cause for you. | Read directly from source; consistent with #424 and #122 |
| 2 | WindowServer burns **13–14 ms of CPU per composited frame**: a new Vulkan descriptor set per draw, a process-wide mutex, Objective-C dispatch | `plugin/nvmtl_vk.c`: `nvmtl_snapshot_set_md`, `g_desc_lock` | A 6.1 ms frame budget at 164 Hz cannot be met, so drags fall to \~35–55 fps | #456 (sampled profile), #356 |
| 3 | The vblank timestamp that CVDisplayLink, CoreAnimation and Chromium phase-lock to passes through **three scheduling hops** before it is stamped | `hwVblIntr` → `thread_call` → `hwVblDeliver` | Even at \~160 submitted fps, motion shows a regular hitch every \~2 s | #424 (RTX 4060 Laptop, macOS 26.7.2) |

**Highest-impact candidate:** stop sleeping under the global display lock while preserving completion and surface lifetimes. This requires gated notifier observations and submission identity; moving the existing `waitFlipDone()` outside the lock is unsafe. The earlier `nullmoth-frame-pacing.patch` is not a deliverable in this source tree. Descriptor allocation and binding-diagnostic overhead can be improved independently without changing the shader binding model.

**Do these three things first:
The User already updated the NullMoth driver to 2.8.1
1. **Remove WhateverGreen.** Your i5-13400F has no integrated GPU, so it does nothing for you, and the README warns it can panic the Mac at login without `gfxrst=4`.
2. **Prove bottleneck 1 on your own machine.** Read the present stats with the Dell connected and then disconnected. The commands are in "What to do on your machine".

> **Caveat:** the patch was written and statically checked in a Linux workspace. It has not been compiled with the macOS kernel toolchain or run on hardware. Treat it as a pull-request-quality proposal for the maintainer, not a drop-in binary.

## Your PC audit, item by item

Your machine boots and accelerates correctly. Three things in the audit work against smoothness: an outdated driver, a mixed-refresh display pair that triggers bottleneck 1, and a hybrid CPU that macOS schedules without knowing which cores are fast. The audit was captured 2026-11-08 09:13 UTC.

| Item | What the audit shows | Assessment | Action |
| --- | --- | --- | --- |
| Driver version | All four `com.nullmoth.*` kexts at **2.7.2** (NVRM, NVAccel, NVRMFB, NVRMAGDC), all versions matching | Current release is **2.8.1**. 2.8 moved to NVIDIA's 620.70.06 open kernel modules and fixed long-session freezes (image lookups no longer scan every image ever created), video-memory exhaustion from large heaps, and added DLSS 4 and Metal 4. | **Update first**, via the 1401 Mac app |
| macOS / SMBIOS | macOS 26.7.2 (25G332), `MacPro7,1`, board-id `Mac-27AD2F918AE68F61` | Correct. macOS 26 refuses `iMacPro1,1`; 1401 uses `MacPro7,1` for desktops with a graphics card because it runs both 15 and 26. | None |
| WhateverGreen | 1.7.2 loaded | Serves no purpose: the i5-13400F has no integrated GPU, and the driver needs no WhateverGreen NVIDIA patches. The README requires `gfxrst=4` whenever WhateverGreen is present, because its boot-screen copy can read freed memory when the NVIDIA framebuffer opens and panic the Mac at the login screen. | Remove it, or add `gfxrst=4` |
| IONDRVSupport | `com.apple.iokit.IONDRVSupport` 600 loaded | On a discrete-GPU desktop, 1401 adds a `Kernel → Block` entry (Strategy `Exclude`) so the firmware framebuffer cannot take display index 0 from NVRMFB. It being loaded suggests the block is missing or disabled. Both your displays are on `NVRMFramebuffer` paths, so it is not visibly hurting you today. | Check the block exists and is `Enabled` |
| CPU topology | brand string i5-13400F; `hw.physicalcpu` 6, `hw.logicalcpu` 16; `CpuTopologyRebuild` 2.0.2 loaded | The 13400F has 6 performance cores (12 threads) plus 4 efficient cores: 10 cores, 16 threads. CpuTopologyRebuild deliberately presents the E-cores as hyper-thread siblings, which is why macOS reports 6 physical cores. macOS has no hybrid-aware scheduler on Intel, so WindowServer's single busy main thread (bottleneck 2) can land on an E-core, which runs it noticeably slower than a P-core. | A/B test with E-cores disabled in BIOS |
| GPU | RTX 4060 Ti, PCI `10DE:2803`, subsystem `1462:5154` (MSI), 8 GB, Metal 3 | In the 620.70.06 device table; driver installed and matched. | None |
| PCIe link | x8 at 16.0 GT/s (Gen 4) | **Correct, not a fault.** The AD106 chip has a native x8 interface. | None |
| BAR1 (Resizable BAR) | Not in the audit | A 256 MB BAR1 is a known source of drag stutter (#462, on an RTX 4060). Your B760 board supports Resizable BAR. | Read `nvrm-bars` with `ioreg`; enable Above 4G Decoding and Resizable BAR in BIOS |
| Displays | ASUS VG248, 1920×1080 @ **164.00 Hz** (main), and Dell SE2218HV, 1920×1080 @ **60 Hz**, both on the NVIDIA card | The exact case that triggers bottleneck 1. The 164.00 Hz value is the mode NVKMS validated from the monitor's EDID timing; do not force an unadvertised rate. | Apply the patch, or test `flip_latch=2` and single-display mode |
| Presentation switches | `debug.nvaccel_iop_flip=1`, `debug.nvrmfb_flip_latch=1` | Defaults: zero-copy scan-out is on, and every flip waits for completion under the display gate. | None for now |
| Memory | 32 GB (2 × 16 GB Samsung DDR5), swap-outs 0 | Ample. The DIMM speed field (5867 MHz) is an SMBIOS display value and has no effect. | None |
| Power | `sleep 0`, `displaysleep 0`, `hibernatemode 0`, `powernap 0` | System sleep off. 2.x has sleep support, so this is a choice, not a requirement. | None |
| Recovery path | No integrated GPU | If the driver fails, the motherboard's video outputs cannot show anything. | Keep the OpenCore USB stick and the **1401: Remove NVIDIA driver** picker entry ready |

The audit does not include your boot arguments. Check that they do not contain `nvhwvbl=0`: in #424 that setting left the vblank on a free-running software timer.

## How the driver works

NullMoth implements Metal on top of Vulkan. Apple's shader bitcode (AIR) is translated to SPIR-V, compiled by Mesa's NVK driver and NAK compiler, and run through NVIDIA's open-source resource manager ported to the macOS kernel (XNU). Rendering throughput, compositor CPU cost and presentation timing must be measured separately; the source analysis alone cannot rule out a rendering bottleneck.

&#91;embedded content: The driver's three paths: render, present, vblank\]

Each display already has its own present worker, but both workers funnel into the same lock. The vblank timestamp is taken only after the fourth step.

The kernel side is four kexts loaded from the Auxiliary Kernel Collection: **NVRM** (NVIDIA's resource manager, firmware load and GPU interrupts), **NVRMFB** (the `IOFramebuffer`: modes, EDID, flips, cursor, vblank), **NVAccel** (the `IOAccelerator` that WindowServer composites through, including zero-copy scan-out) and **NVRMAGDC** (Apple's display-policy queries). On the user side, `NVMTLDriver.bundle` is the Metal driver that Metal.framework loads for the card.

## Root cause 1: every display's flip wait sleeps under one global lock

With two displays, the slow display's flip wait freezes the fast display's frames and the cursor for most of each slow refresh. This is visible directly in the source, and it is the bottleneck your 164 Hz + 60 Hz pair hits hardest.

### What the code does

The framebuffer kext's "display gate" is one static recursive lock for the whole driver, not one per display (`nvrm-fb.cpp`, lines 45–58 and 1739 in 2.8.1):

```cpp
static IORecursiveLock *startLock();          // one lock, process-wide
class NVRMDisplayGate {
    IORecursiveLock *lock;
public:
    NVRMDisplayGate() : lock(startLock()) { if (lock) IORecursiveLockLock(lock); }
    ~NVRMDisplayGate() { if (lock) IORecursiveLockUnlock(lock); }
};
```

Every zero-copy frame WindowServer presents goes through `flipToMemory()`, which takes that gate on its first line and holds it until it returns. Inside it, `submitConfig()` commits the flip to NVKMS and then sleeps until the flip has reached the panel:

```cpp
bool NVRMNVDAFramebuffer::flipToMemory(...) {
    NVRMDisplayGate gate;                 // global lock taken here
    ...
    NvBool ok = submitConfig(cfg, rep, NV_TRUE, false, false, true);
    ...                                   // released only when this returns
}
// in submitConfig(), after applyModeSetConfig():
if (waitFlipDone(flipWaitUs())) { ... return NV_TRUE; }   // sleep: up to 1.25 refresh periods
for (;;) { if (waitFlipDone(flipWaitUs())) return NV_TRUE;  // late: keep sleeping
           if (elapsed >= kFlipStuckUs /* 100 ms */) break; }
```

`flipWaitUs()` is one refresh period plus a quarter. `waitFlipDone()` sleeps on an `IOLock` event that the flip-done callback signals, re-checking every 250 µs. It releases its own small lock while asleep, but **not** the display gate. Everything else that needs the gate waits behind it: the other display's flips, `cursorApply` and `setCursorImage`, mode sets, and display sleep and wake.

### What it costs on your two displays

|  | VG248 @ 164 Hz | Dell @ 60 Hz |
| --- | --- | --- |
| Refresh period | 6.10 ms | 16.67 ms |
| Flip wait bound (`flipWaitUs`, 1.25 × period) | 7.6 ms | 20.8 ms |
| Typical time a flip holds the gate (commit lands at a random point; wait ≈ time to next vblank) | \~3 ms | \~8 ms |

Each time the Dell presents, the gate is held for \~8 ms on average. During that time a VG248 frame (6.1 ms budget) cannot be submitted, so it misses its vblank and the previous frame is shown twice. This repeats at the Dell's rate whenever both screens are updating, for example while a window or the cursor moves across them, or while video plays on one. The result is irregular judder on the fast screen and a cursor that skips. The numbers above are an estimate from the code; the measurement that confirms it is in "What to do on your machine".

### Why it was built this way

The wait itself is correct and must stay. Until the new flip has latched, the panel may still be scanning out the old front surface, and handing that surface back to WindowServer early causes a strobe or a torn frame. The problem is only *where* the wait happens. NVAccel already gives each display its own asynchronous worker (`gIopAs[head]` in `nvrm-accel.cpp`, completing through `signalTransactionInterrupt`, the same shape Apple's drivers use). That worker is the safe place to sleep: it serves one display, it is already serialised by a busy flag, and it completes WindowServer's transaction only after the flip returns.

### Related reports

- \#424: 165 Hz panel plus a second display measured at 28.98 Hz with an 18.4 ms standard deviation; regular hitches.
- \#122: flicker on boot and mode changes, which stopped when the second monitor was disconnected.
- The driver's own comment in `vblFire()` describes a 75 Hz + 60 Hz pair where the second screen jittered; that fix addressed vblank registration, not the shared lock.

## Root cause 2: WindowServer's CPU cost per frame

Historical laptop reports measured 13–14 ms of WindowServer CPU time per composited frame, exceeding the 6.1 ms budget at 164 Hz. That aggregate alone does not mathematically explain a 35–44 fps rate or isolate one function's cost; profiling and an A/B test are needed. The binding layer contains independently reproducible unnecessary work fixed below.

### Measurements from the issues

- **#456** (RTX 3060 Laptop, 165 Hz, macOS 26.7.1, driver 2.6.3). WindowServer CPU time divided by the number of flips gives 13.65, 12.65 and 14.12 ms per frame over three runs, at 35–38 flips/s. The GPU was 7–12% busy. `debug.nvrmfb_flip_latch=2` made no measurable difference, which rules out the flip wait as the limit on that single-display machine.
- **#356** (same class of machine, driver 2.5.3). Dragging any window settles at \~50 presents/s and is nearly independent of refresh rate (60 Hz gave 50/s), window size (300×200: 56/s; 1400×1000: 54/s) and shadow (57/s without). Cost per frame is roughly fixed at 18–20 ms, and WindowServer is pegged at \~100% of one core.

### The sampled profile (#456, `sample WindowServer 8` during a drag)

`ws_main_thread` took 5,412 of 8,000 samples. The hot chain runs `CGXUpdateDisplay` → `WS::Updater::UpdateDisplays` → `CDDisplay::render_update` → `CompositorMetal::composite` → `CA::OGL::MetalContext::draw` → `-[NVMTLRenderCommandEncoder drawIndexedPrimitives:…]`. The leaf costs underneath are:

| Leaf function | Samples | What it is |
| --- | --- | --- |
| `objc_msgSend` / retain / release | \~322 | Objective-C dispatch and reference counting on every encoder call |
| `nvk_UpdateDescriptorSets` | 203 | Writing a Vulkan descriptor set for the draw |
| `memmove` | 117 | Copying binding state |
| `nvk_AllocateDescriptorSets` | 90 | Allocating a new descriptor set |
| `nvmtl_snapshot_set_md` | 69 | The plugin's "snapshot the current bindings into a set" routine |

### Why the code is slow here (`plugin/nvmtl_vk.c`)

1. **Metal's binding model is emulated with large descriptor layouts.** The plugin snapshots bindings into sets whose recorded/in-flight users must not observe later rebinding. Vulkan descriptor sets are not inherently immutable; the plugin's lifetime/update rules are why it makes snapshots.
2. **CoreAnimation changes textures on almost every draw.** It draws one layer at a time (window contents, shadow, title bar, cursor), each with its own texture. The reuse fast paths in `nvmtl_snapshot_set_c()` only apply when the textures are unchanged or only buffer slots changed, so most compositor draws fall through to a new set plus `vkUpdateDescriptorSets`.
3. **Allocation goes through a process-wide mutex on batch misses, not every draw.** The default command-local batch already has 32 sets. A demonstrated defect opened a new pool before trying a single set in existing pools when a batch did not fit; list growth also called `realloc` under `g_desc_lock`. The local patch fixes these two unnecessary slow paths.
4. **The fast Vulkan alternative is already enabled but unused.** `VK_KHR_push_descriptor` is requested at device creation (`nvmtl_vk.c` around line 981), but only so NVIDIA's DLSS runtime can bind its own passes. The Metal draw path never uses it.

### Fix options, lowest risk first

| Option | What changes | Expected gain | Risk |
| --- | --- | --- | --- |
| A. Push descriptors for small pipelines | For pipelines whose shaders touch only a few bindings (typical CoreAnimation shaders use a handful of textures and buffers), build a layout with `VK_DESCRIPTOR_SET_LAYOUT_CREATE_PUSH_DESCRIPTOR_BIT_KHR` and record bindings with `vkCmdPushDescriptorSetWithTemplateKHR`. No pool, no mutex, no allocate or free. Everything else keeps the current path. | Removes the allocate, update and lock costs for most compositor draws (to be measured) | Low. Must respect the device's `maxPushDescriptors` limit and fall back above it. |
| B. Descriptor update templates | Replace `VkWriteDescriptorSet` arrays with one `VkDescriptorUpdateTemplate` per pipeline layout | Cuts the per-draw struct building and `memmove` | Low |
| C. Objective-C hot-path trimming | Cache method implementations (IMP caching) for encoder calls made per draw, use `objc_direct` methods internally, and avoid retain/release on internal paths | Part of the \~322 samples | Low |
| D. Bindless texture heap | Write each texture's view once, when it is created, into a large descriptor array, and pass texture indices to shaders via push constants. Maps naturally to NVIDIA's texture header pool. | Largest: no descriptor writes per draw at all | Medium. Needs translator changes so `[[texture(n)]]` reads `heap[index[n]]`. |
| E. `VK_EXT_descriptor_buffer` | Descriptors become plain memory written into a ring buffer | No pools or locks | Medium. Depends on the extension's maturity in the pinned Mesa/NVK. |

Do not ship A + B + C solely from this analysis. Push descriptors require a compatible layout, device limits, command entry points and coverage of every descriptor type; the current full layout and dynamic-buffer bindings cannot simply be flagged as push descriptors. The Vulkan specification prohibits dynamic uniform/storage-buffer descriptors in push layouts. The applied allocator patch retains the existing binding model. Its end-to-end gain still requires `sample WindowServer` and CPU-time-per-flip measurements on hardware.

**Related, #370:** WindowServer also spends 30–60% of a core translating CoreAnimation shaders the first time it uses them. That cost shows up as stutter that is worse after each boot. The fix is to prewarm WindowServer's shader cache at install time: the set of CoreAnimation pipelines is fixed per macOS build, so it can be translated once and kept.

## Root cause 3: vblank timestamp jitter and phase

Even when nearly every frame is delivered, motion can look uneven because the timing signal that apps and the compositor pace themselves against arrives late and with variable delay. This explains reports where the frame counter looks fine but the desktop does not.

### Measurements from #424

RTX 4060 Laptop (`0x28e0`), macOS 26.7.2, driver 2.6.2, 1920×1080 @ 165 Hz over DisplayPort (pixel clock 435.6 MHz, 2200 × 1200 total):

- 160.5 submitted fps while dragging (97% of refresh), 82.4 fps idle.
- Average flip-latch latency 3.57 ms, 59% of the 6.06 ms period. Submissions land at arbitrary phase within the frame rather than just after a vblank.
- In 50 ms windows, 53 had 8 frames and 54 had 9, as expected; 4 had only 4–7 frames. That is roughly one visible hitch every 2 seconds.
- The driver log showed the vblank source was the software timer, because `nvhwvbl=0` was set. A second display read 28.98 Hz with an 18.4 ms standard deviation.
- The reporter noted that 165 Hz and even 280 Hz "look almost identical to 60 Hz".

### Mechanism

macOS's `IOFramebuffer` stamps each vblank with the time at which the driver calls its VBL interrupt procedure: it records `vblTime` and `vblDelta` in shared memory at the moment of the call. CVDisplayLink, CoreAnimation's display timing and Chromium's BeginFrame source all phase-lock to that stamp. On real Apple hardware the procedure runs at interrupt time, so the stamp is accurate. In this driver the vblank passes through three scheduling hops first:

1. GPU MSI → `IOFilterInterruptEventSource` → NVRM's work loop runs `rm_isr()` and `rm_isr_bh()` (`NVRM.cpp`, `interruptOccurred`).
2. NVIDIA's resource manager → NVKMS → the framebuffer's callback `hwVblIntr()`, which records `fHwVblLast` and schedules a thread call.
3. The thread call runs `hwVblDeliver()`, which finally invokes the VBL procedure.

Each hop can be delayed by whatever else is running: the RM work loop is shared with all GPU interrupt work, and thread calls wait for a free kernel thread. A late stamp shifts the phase estimate that everything downstream uses, so frames get scheduled for the wrong moment and occasionally land one refresh late. When hardware vblank is not registered, `vblFire()` drives the procedure from a free-running timer whose phase has no fixed relation to the real scan-out.

In current code, hardware vblank is on by default (`nvhwvbl` defaults to 1). Registration is deferred until NVKMS has latched a flip on that head or the mode has been set for \~60 timer ticks, and a refused registration is retried up to 12 times. Your log should show `hw vblank on head N: REGISTERED` for both heads.

### Fix

- **Measurement remains open.** The previously proposed `nvrmfb_vbl_deliver_max_us` / `nvrmfb_vbl_deliver_late` counters are not implemented by these patches. Callback-to-delivery latency and true interrupt-to-stamp latency must be distinguished before changing the timing source.
- **Do not bypass hop 3 without proving callback context.** No `nvrmfb_vbl_direct` shortcut is implemented here. An unchecked direct call could re-enter IOGraphics while RM/NVKMS locks are held.
- **Later** (roadmap). Take the timestamp in the MSI filter (primary interrupt context), where it is closest to the real event. Discipline the fallback timer to flip-completion times, like a phase-locked loop, so even the timer path tracks the panel.

## High and mixed refresh, browser acceleration, video

High-refresh modes themselves work: the driver publishes every rate NVKMS validates, and the display-compression (DSC) black-screen fix is documented in driver 1.14. What limits high refresh is sustaining those rates, which is the three bottlenecks above. Browsers inherit the same limits, and video playback in browsers is weakened by missing VP9/AV1 hardware decode.

| Topic | State in 2.8.1 (from source) | What it means for you | Fix |
| --- | --- | --- | --- |
| Offering refresh rates | `addKmsRefreshModes()` publishes every rate NVKMS validated for a raster macOS knows. Timings come straight from NVKMS. | Your VG248's 164.00 Hz is the rate its EDID timing produces. | Nothing; do not force rates the EDID does not advertise |
| Mixed refresh (164 + 60 Hz) | One global display gate (root cause 1) | Judder on the fast screen whenever both update | The patch; later, per-display locks |
| VRR / Adaptive-Sync | Not implemented. `getAttributeForConnection()` exposes no variable-refresh attribute. | Fixed refresh only | Roadmap P2 |
| 10-bit / HDR desktop | Connection reports 8 bits per channel (`kConnectionColorDepthsSupported = 2`); your displays run `CGSThirtytwoBitColor`. 12-bit HDMI deep colour exists only behind `nvmaxbpc=12`. | 8-bit SDR desktop | Roadmap P2 |
| Chromium browsers (Chrome, Edge, Brave, Arc, Vivaldi, Opera) | Rendered through ANGLE's Metal back end; the plugin recognises these processes by name or loaded libraries. WebGL shader-translation failures were fixed through 2.6.x. | Scroll smoothness depends on the BeginFrame timestamp (root cause 3) and WindowServer's per-draw cost (root cause 2) | Patch + P1 work |
| Safari | WebKit's GPU process uses Metal; sandboxed Apple apps hand video decoding to `VTDecoderXPCService` | Same as above | Same |
| Hardware video decode | H.264 and HEVC through NVDEC (`libnvdec_h264*.dylib`, registered with VideoToolbox). **No VP9 or AV1**: the source has no AV1 code at all. | YouTube mostly serves VP9/AV1, so your CPU decodes it in software and competes with WindowServer for the same cores | Now: force H.264 with an extension such as h264ify. Later: register AD106's NVDEC VP9/AV1 with VideoToolbox. |
| Hardware video encode | H.264 and HEVC through NVENC | No AV1 encode | Add Ada's AV1 NVENC (P2) |

Two browser-specific notes. First, the changelog records a Chromium scrolling improvement in 2.6.3 (from the NvGfx work) and WebGL fixes since; staying current matters more than any flag. Second, `#ignore-gpu-blocklist`-style flags do not help here: acceleration is already on, and the limits are in the driver's presentation and CPU paths.

## Full RTX 4060 Ti (AD106) support checklist

Your card is supported today. It is in the device table, NVIDIA's firmware and resource manager cover it, and the shader compiler targets it. But the project has not physically validated any Ada card: `docs/CARD-SUPPORT.md` lists Ada as "Pending", and most testing is on an RTX 5060. "Full support" means every row below is green.

| Area | Status | Notes and action |
| --- | --- | --- |
| PCI ID `10DE:2803` | Done | One of 249 IDs from NVIDIA 620.70.06 (`SUPPORTED_IDS` in `package/install.sh`) |
| GSP firmware and resource manager | Done | NVIDIA's open kernel modules (r620) support Turing and later, Ada included |
| Shader compiler (NVK/NAK, sm\_89) | Done | The optional vendor compiler picks the SM target from the chip name and is tested for Ada; unknown chips fall back to NAK |
| Metal 3 and Metal 4 GPU families | Done | Metal 4 objects added in 2.8 |
| DLSS Super Resolution / Frame Generation / Ray Reconstruction | Done (opt-in) | `sudo env NULLMOTH_DLSS=1 ./install.sh` downloads NVIDIA's files and compiles kernels for your card (about two minutes). Ray Reconstruction needs macOS 26, which you have. |
| 8 GB VRAM handling | Done | Large heaps grow in 64 MB pieces (2.8). Cyberpunk 2077's ray-tracing threshold rule answers 6200 MB on cards with at least 7.2 GB, which includes yours |
| PCIe link | Correct | x8 Gen 4 is AD106's native width |
| Resizable BAR / 8 GB BAR1 | **Verify** | 256 MB BAR1 causes drag stalls (#462, RTX 4060). B760 supports Resizable BAR |
| Two displays at different refresh rates | **Improve** | Root cause 1; the patch |
| Smooth desktop at 144 Hz and above | **Improve** | Root causes 2 and 3 |
| NVDEC AV1 and VP9 decode | **Missing** | The hardware decodes both; the driver registers only H.264/HEVC |
| NVENC AV1 encode | **Missing** | New in Ada's encoder; driver encodes H.264/HEVC only |
| Variable refresh (G-SYNC Compatible) | **Missing** | Roadmap P2 |
| 10-bit / HDR desktop | **Missing** | Roadmap P2 |
| Physical validation on Ada | **Not done** | You can help directly: run 1401's Probe and **Send logs**, and post the report ID with "AD106, 164 Hz + 60 Hz" |

With several monitors attached, NVIDIA cards keep memory clocks high at idle to avoid display glitches. If idle power matters, NVIDIA's own option `nvregdwords=RmDisableDisplayGlitchPerfLimit=1` lowers it, at the cost of a brief flicker when clocks change.

## Applied patches: implementation and verification

All changes are in existing files. The external links and issue descriptions were checked against the local source; the workspace itself has no `.git` metadata, so the downloaded source snapshot is not cryptographic build provenance.

| Area | Applied change | Validation boundary |
| --- | --- | --- |
| Mixed-refresh presentation | `kexts/NVRMFB/fb/nvrm-fb.cpp`: private epoch/sequence token; notifier observations stay display-gated while the pure non-HOME landing wait sleeps outside the gate. The platform function still returns synchronously only after completion. | Experimental, **default off**: `debug.nvrmfb_flip_ungated=1`. No patched-kext boot or pacing measurement yet. |
| High-refresh fallback | The fallback timer accepts the same 1–50 ms timing range as `flipWaitUs()`. Validated 540/600 Hz timing arithmetic no longer silently falls back to 60 Hz. | Extracted production function tested through 1000 Hz; this does not advertise unvalidated monitor modes. |
| Descriptor allocation | `plugin/nvmtl_vk.c`: on a batch miss try one set in existing pools before opening another; grow the command-local set list outside the global pool mutex; do not retry non-capacity Vulkan errors. | Production allocator fixtures cover pool tails, fragmentation, pool limit, layout switches and host OOM. No push-descriptor layout change. |
| Binding overhead, issue #531 | `plugin/NVMTLObjects.m`: literal decimal sampler keys replace per-bind number-to-string conversion; a locked 128-entry primitive shape cache replaces repeated diagnostic NSString formatting. | Production setter/diagnostic fixtures count zero valid-index Foundation formatting calls; preserve LOD, nil, failure and saturation behavior. No claimed FPS gain. |
| Browser acceleration policy | `plugin/NVMTLDevice.m`: dynamically size the ANGLE override string instead of silently truncating it at 1024 bytes; preserve the caller's overrides and log only a successful update. | Tests cover 1000/1023/1100-byte existing values and existing opt-out policy. Browser WebGL/video playback on the patched driver is not certified. |
| Plugin build | `build/build_plugin.sh`: link IOKit/CoreFoundation explicitly and target macOS 15, matching the release builder. | The previous link failed on direct IOKit symbols. The corrected release plugin compiles/links with minos 15.0. |
| Ada selection | Existing `2803`/`2805` acceptance and AD102/103/104/106/107 `sm_89` selection already work. Expanded the existing support test; did not add guessed device IDs. | CPU coverage plus limited live RTX 4060 Ti tests below, not complete feature certification. |

### Completion and lifetime invariants

The original split platform ABI proposal is deliberately not implemented. There are no new request flags, no new platform function, no accelerator ABI change, and no unchecked direct VBL callback. A patched framebuffer therefore works with the existing accelerator's synchronous platform call contract.

- Acceptance is not exposed as completion. A private token is passed only within the framebuffer, for non-HOME pure presents with latch mode 1 and the experimental switch enabled.
- All primary completion observations publish the completed sequence before a later submission may advance/reset the live notifier. A waiter must not mistake a newer notifier for its own flip.
- Each poll takes the display gate briefly; device/notifier access and the shared present-stat message remain protected. The 250 microsecond event sleep occurs with no display gate held.
- STOP/power invalidation changes the epoch, and unresolved work faults closed rather than becoming a false success. The framebuffer's existing duplicate-memory/surface pins survive failure and stop; timeout is not permission to destroy a potentially scanned-out surface.
- Mode/HOME submissions, latch modes 0/2 and the default-off setting retain gated behavior. This is not a general replacement of display topology locking.
- The supported teardown ordering is IOKit child-stop before parent adapter shutdown. Arbitrary adapter-loss recovery and NVKMS notifier semantics under all power transitions still require hardware validation.

### Tests and builds executed

The revised existing `test_flip_latch_counters.py` lifts the actual platform branch, flip/submit functions and waiter. It exercises mixed-head progress during sleeps, acceptance versus landing, later notifier advance/rejection, STOP/power/device/fault cases, HOME/mode paths, late/stuck handling and pinned lifetimes. Negative controls restoring the old gated path or removing completion publication fail as required. Existing present-stat, async-present and sysctl-review tests pass. These are controlled fixtures, not real-time display measurements.

The existing high-refresh regression was first observed failing at 540 Hz, then passed after the floor fix; restoring either the 2 ms or 4 ms floor fails. Descriptor allocation, long ANGLE override and binding-formatting regressions were likewise reproduced before their fixes. The broad run executed 87 existing Python scripts: 84 passed after fixing the sampler fixture's new lock-header dependency; three remain blocked (`test_gl_translation.py` needs a translator library and SPIR-V tools; `test_resource_phi_fold.py` needs a built Rust SPIR-V dependency; `test_nvrm_map_table.py` requests historical commit `042bebe`, which is absent here and not resolvable from upstream). Public v1.15.0/39d70bc history was fetched into scratch Git storage to run the other negative controls, without adding `.git` to this workspace. A new standby regression also caught and fixed an experimental-path regression: only private landing waits may fault on invalidation, not legacy bootstrap/latch=0/2 submissions.

Build verification uses Xcode's macOS SDK, official Khronos Vulkan-Headers v1.4.304 and NVIDIA open-gpu-kernel-modules 620.70.06 downloaded into scratch directories, without changing system installations. The Metal plugin is linked as x86_64 with a macOS 15.0 deployment target. The framebuffer compiles against real kernel and NVKMS headers and links as an x86_64 Mach-O kext; complete package/auxiliary-kernel-collection validation is separate. The existing accelerator build script cannot run from clean upstream headers alone: it expects generated Darwin RM definitions at `src/nvidia/_out/Darwin_x86_64/compile_cmds.sh`.

### Limited live RTX 4060 Ti evidence

A read-only host query identifies a real `10de:2803` RTX 4060 Ti/AD106 with 8 GB and 164 Hz + 60 Hz displays. The currently loaded kexts report 2.8; they are **not** the newly compiled framebuffer. No kext, boot configuration or installed plugin was replaced.

The newly built plugin was loaded explicitly in disposable test processes over the installed driver stack. All eleven existing `bench_surface_order.m` modes passed 100 frames each (queued, same, private, cross, alias, alias_read, partial, partial_alias, abandoned, cpu and wait): **1100 frames, zero bad frames**. `bench_copy_coherence.m` passed 16 iterations with zero host-to-texture or texture-to-host mismatches. This validates these copy/clear/publication cases on AD106, not shader conformance, WindowServer performance, browser playback or the patched kernel path.

### Hardware acceptance still required

Do not install the experimental framebuffer as a claimed finished, bug-free driver. Keep the recovery boot entry available; test only with a matched, backed-up package. After a test build is installed and rebooted, compare `debug.nvrmfb_flip_ungated=0` and `=1` with latch mode 1, using 8-second drags on the 164 Hz head while the 60 Hz head also updates. Record flips/s, skipped/late frames, CPU time per frame and failure counters. Then verify HOME transitions, all supported refresh/resolution changes, unplug/replug, display sleep, system sleep/wake and a long stable WindowServer session. No direct-VBL experiment is needed or enabled.

VRR, HDR/10-bit desktop, VP9/AV1 VideoToolbox registration and AV1 NVENC remain unimplemented. Neither the CPU regressions nor the limited AD106 probes close those feature gaps.

## Roadmap

The order follows risk and payoff. The small, switchable presentation fix goes first, because it is cheap to revert and directly measurable. The descriptor work comes next; it is larger but still only adds a fast path. New codecs, variable refresh and HDR come last.

&#91;embedded content: Roadmap · three phases with exit gates\]

Each change should sit behind a sysctl, as the P0 patch does, so each phase is proven by an on/off A/B test on the same machine rather than by impression. The P1 gate in numbers: at 164 Hz, WindowServer's CPU time per frame must fall below 6.1 ms. #456 measured 13–14 ms on a laptop. P2's per-display locks generalise the patch: the gate would cover only topology changes, and each display's flip and cursor state would get its own lock.

## What to do on your machine

Change one thing at a time and re-measure after each step, so you know what helped. Before starting, make sure your OpenCore USB stick boots, and keep a copy of `config.plist`.

### 1. Take a baseline

Drag a window continuously for about 10 seconds across the VG248, then run:

```bash
# Per-display present statistics from the framebuffer (one line per 10 s window of activity)
log show --last 2m --predicate 'eventMessage CONTAINS "present stats"' | tail -6

# Flip-wait counters (cumulative since boot; take the difference between two reads)
sysctl debug.nvrmfb_flip_latch_waits debug.nvrmfb_flip_latch_late \
       debug.nvrmfb_flip_latch_timeouts debug.nvrmfb_flip_latch_max_us \
       debug.nvaccel_iop_async_drop debug.nvaccel_iop_late debug.nvaccel_iop_flips

# Hardware vblank registered on both heads?
log show --last 1d --predicate 'eventMessage CONTAINS "hw vblank on head"' | tail -4

# BAR sizes and placement
ioreg -l -w0 | grep -E 'nvrm-bars|nvrm-bar1-placed'

# Boot arguments in effect
nvram boot-args
```

**How to read it:**

| Reading | Healthy | Points to |
| --- | --- | --- |
| `present stats head N: X flips ... (Y/s, panel Z Hz)` during a drag | Y close to Z on the VG248 head | Y far below Z: one of the three bottlenecks |
| "...presents skipped for a flip still pending" in that line, and `nvaccel_iop_late` | Near 0 | Rising during drags: flips land late (root cause 1 or 3) |
| `nvaccel_iop_async_drop` | Near 0 | Frames replaced while a flip was in flight |
| `hw vblank on head N: REGISTERED` | One line per display | "REFUSED" or absent: the timer drives vblank (root cause 3) |
| `nvrm-bars` BAR1 size | About 8 GB | 256 MB: enable Resizable BAR (see #462) |
| `boot-args` | Contains `nvfb=1 nvaccel=1 nvfbheads=4` and the AMFI arguments; no `nvhwvbl=0`; no `nv_disable=1` | Fix in OpenCore's `config.plist`, then reboot |

### 2. Make these changes in order

- [ ] **Update to driver 2.8.1** with the newest 1401 Mac app (Update or Install). One restart is enough for an update. Then check that `kmutil showloaded --list-only | grep nullmoth` shows 2.8.1 on all four kexts.
- [ ] **Remove WhateverGreen.** Delete `EFI/OC/Kexts/WhateverGreen.kext` and disable its `Kernel → Add` entry in `config.plist`. If you keep it for any reason, add `gfxrst=4` to `boot-args`.
- [ ] **Check the IONDRVSupport block.** `Kernel → Block` should contain `com.apple.iokit.IONDRVSupport` with Strategy `Exclude` and `Enabled` true. Running 1401's Install or Repair rewrites it correctly.
- [ ] **Confirm Resizable BAR.** In BIOS: Above 4G Decoding on, Resizable BAR on, CSM off. Do not copy OpenCore BAR quirk values from someone else's board; 1401 keeps yours and the driver places BAR1 itself.
- [ ] **Prove root cause 1.** Repeat the baseline with the Dell disconnected (or disabled in Displays). If the VG248's flips/s rises clearly and "skipped" falls, the shared lock is confirmed on your machine. That is the strongest evidence you can attach to an issue.
- [ ] **Interim workaround for two displays.** `sudo sysctl debug.nvrmfb_flip_latch=2` (the opt-in from #300; it resets at reboot). The next submit then verifies the previous flip instead of every flip sleeping after commit, which shortens how long the gate is held. Watch for flicker; if any appears, set it back to 1. On the single-display machine in #456 it changed nothing, because that machine was limited by root cause 2.
- [ ] **E-core A/B test.** Disable the E-cores in BIOS (6 P-cores, 12 threads) and repeat the drag test. If drags get smoother, WindowServer was landing on E-cores. Weigh that against the multi-threaded performance you lose. With E-cores off, CpuTopologyRebuild is no longer needed.
- [ ] **Hardware-decode YouTube.** Install an h264ify-type extension in Chrome so YouTube serves H.264, which the card decodes. Check in Activity Monitor that the browser's CPU use drops during playback.
- [ ] **Reduce compositor work.** A static wallpaper and System Settings → Accessibility → Display → Reduce transparency cut the number of draws per frame. This is a temporary mitigation for root cause 2.
- [ ] **Report your results.** In 1401: Crash report → Send logs, then post the report ID with "RTX 4060 Ti (AD106), 164 Hz + 60 Hz, macOS 26.7.2" on #424 or a new issue. Write it in your own words: the reporters there note that the project closes AI-written issues without review.

### 3. If you build the patched driver yourself

The existing source files already contain the local patches; there is no `nullmoth-frame-pacing.patch` to apply. Use the README's pinned dependencies and a complete kernel build environment, including generated Darwin RM definitions. Keep the **1401: Remove NVIDIA driver** recovery entry. A framebuffer ABI change is not required by this revision; nevertheless validate the matched package and kernel collection before installation. After a controlled test boot, A/B `debug.nvrmfb_flip_ungated=0` and `=1` with latch mode 1. Do not enable nonexistent direct-VBL switches.

## Limits of this analysis and open questions

Source findings identify candidate bottlenecks, not their measured contribution to every setup. The limited AD106 plugin probes above ran on this host, but no patched-kext boot, window-drag benchmark, browser playback measurement or complete GPU feature matrix was performed.

- **Source observations, not hardware diagnoses:** the display gate is shared across displays; the default flip wait sleeps while holding it; push descriptors are requested but not used by Metal draws; the hardware-vblank callback schedules a thread call; the VideoToolbox path has no AV1/VP9 registration. The opt-in pacing patch below changes the pure-present wait, not the default behavior.
- **Estimated:** the \~8 ms average gate hold per 60 Hz flip, and how much each fix gains. The timing table assumes commits land at random phase.
- **From other people's machines:** the CPU-per-frame numbers (#456, #356) and the phase measurements (#424) come from RTX 3060 and RTX 4060 laptops, not a desktop RTX 4060 Ti.
- **Not checked:** whether NVKMS callback context permits direct VBL delivery; no direct-VBL option is implemented.
- **GitHub coverage:** the original analysis used selected issue pages; the implementation review additionally read the public API open-issue list and issue #531. This is not an exhaustive fix for every open GitHub issue.
- **Audit limits:** the original audit lacked boot arguments, BAR1 size and kernel logs. Current read-only queries confirm the GPU/display identities, but do not substitute for A/B presentation measurements.

## Sources

- Source code: [nullmoth/nvidia-macos-driver](https://github.com/nullmoth/nvidia-macos-driver), commit `41a6494` (2.8.1): `kexts/NVRMFB/fb/nvrm-fb.cpp`, `kexts/NVRM/accel/nvrm-accel.cpp`, `kexts/NVRM/NVRM.cpp`, `kexts/NVRM/os-nvkms-xnu.cpp`, `plugin/nvmtl_vk.c`, `plugin/NVMTLObjects.m`, `plugin/NVMTLEventSync.m`, `plugin/NVMTLDevice.m`, `README.md`, `CHANGELOG.md`, `docs/HOW-IT-WORKS.md`, `docs/TROUBLESHOOTING.md`, `docs/CARD-SUPPORT.md`
- Source code: [nullmoth/1401](https://github.com/nullmoth/1401), commit `6474c03`: `mac/Resources/nullmoth-setup.sh` (IONDRVSupport block logic)
- [NullMoth Systems](https://nullmothsystems.com): current version 2.8.1, supported cards
- [#456 WindowServer spends 13-14 ms of CPU per presented frame](https://github.com/nullmoth/nvidia-macos-driver/issues/456)
- [#424 Frame submission is not phase-locked to vblank at 165 Hz](https://github.com/nullmoth/nvidia-macos-driver/issues/424)
- [#462 X99: WindowServer lag when dragging windows on RTX 4060 (256 MB BAR1)](https://github.com/nullmoth/nvidia-macos-driver/issues/462)
- [#356 Moving any window caps at \~50 fps on a 165 Hz panel](https://github.com/nullmoth/nvidia-macos-driver/issues/356)
- [#370 WindowServer spends 30-60% of a core translating Core Animation shaders](https://github.com/nullmoth/nvidia-macos-driver/issues/370)
- [#122 RTX 2070 Super: persistent flickering on boot and display mode changes](https://github.com/nullmoth/nvidia-macos-driver/issues/122)
- Your `pc-audit-final.json`, captured 2026-11-08 09:13 UTC
