/* Chromium hands rendered IOSurfaces to another process from scheduled callbacks (upstream PR #481).
 * Publish these writes before reporting scheduled: the consumer has no Vulkan queue dependency on this
 * process, and shared VRAM can still be in flight. Reporting earlier made whole pages flicker in Chrome/Edge. */
#pragma once
#include <string.h>

static inline int nvmtl_surface_schedule_after(const char *setting, const char *process)
{
    /* Preserve the existing explicit 0..3 diagnostic policy. */
    if (setting && *setting >= '0' && *setting <= '3' && !setting[1]) return setting[0] - '0';
    if (process && (strstr(process, "Chrome") || strstr(process, "Chromium")
                    || !strncmp(process, "Microsoft Edge", 14) || !strncmp(process, "Brave Browser", 13)
                    || !strncmp(process, "Vivaldi", 7))) return 1;
    return 3;
}
