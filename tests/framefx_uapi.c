#include <stddef.h>
#include "../linux/uapi/framefx.h"

_Static_assert(sizeof(struct framefx_caps) == 32, "caps size");
_Static_assert(sizeof(struct framefx_buffers) == 32, "buffers size");
_Static_assert(sizeof(struct framefx_submit) == 40, "submit size");
_Static_assert(sizeof(struct framefx_wait) == 48, "wait size");
_Static_assert(offsetof(struct framefx_buffers, source_offset) == 16, "source offset");
_Static_assert(offsetof(struct framefx_submit, job_id) == 32, "job offset");
_Static_assert(offsetof(struct framefx_wait, cycles) == 24, "cycles offset");
_Static_assert(offsetof(struct framefx_wait, irq_count) == 40, "irq offset");
_Static_assert(FRAMEFX_QUERY_CAPS == 0x80204600U, "query number");
_Static_assert(FRAMEFX_ALLOC_BUFS == 0xc0204601U, "allocate number");
_Static_assert(FRAMEFX_SUBMIT == 0xc0284602U, "submit number");
_Static_assert(FRAMEFX_WAIT == 0xc0304603U, "wait number");
_Static_assert(FRAMEFX_RESET == 0x4604U, "reset number");
