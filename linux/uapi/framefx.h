#ifndef FRAMEFX_UAPI_H
#define FRAMEFX_UAPI_H

#include <linux/ioctl.h>
#include <linux/types.h>

#define FRAMEFX_ABI_VERSION 1
#define FRAMEFX_MAX_BUFFER (2U * 1024 * 1024)
#define FRAMEFX_DESTINATION_OFFSET (16U * 1024 * 1024)
#define FRAMEFX_MAX_WAIT_MS 30000U

struct framefx_caps {
	__u32 abi_version, hardware_version, capabilities;
	__u32 max_width, max_height, stride_alignment, max_buffer_bytes, reserved;
};

struct framefx_buffers {
	__u32 source_bytes, destination_bytes, reserved[2];
	__aligned_u64 source_offset, destination_offset;
};

struct framefx_submit {
	__u32 width, height, source_stride, destination_stride, mode, threshold;
	__u32 reserved[2];
	__aligned_u64 job_id;
};

struct framefx_wait {
	__aligned_u64 job_id;
	__u32 timeout_ms, reserved;
	__u32 status, error_code;
	__aligned_u64 cycles;
	__u32 bytes_read, bytes_written;
	__aligned_u64 irq_count;
};

#define FRAMEFX_QUERY_CAPS _IOR('F', 0, struct framefx_caps)
#define FRAMEFX_ALLOC_BUFS _IOWR('F', 1, struct framefx_buffers)
#define FRAMEFX_SUBMIT _IOWR('F', 2, struct framefx_submit)
#define FRAMEFX_WAIT _IOWR('F', 3, struct framefx_wait)
#define FRAMEFX_RESET _IO('F', 4)

#endif
