#include <linux/clk.h>
#include <linux/compat.h>
#include <linux/completion.h>
#include <linux/delay.h>
#include <linux/dma-mapping.h>
#include <linux/interrupt.h>
#include <linux/io.h>
#include <linux/kref.h>
#include <linux/miscdevice.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/version.h>
#include "framefx.h"

#define FX_MAGIC 0x46524658
#define FX_CONTROL 0x00c
#define FX_STATUS 0x010
#define FX_IRQ_STATUS 0x03c
#define FX_IRQ_ENABLE 0x040

struct fx_buffer {
	void *cpu;
	dma_addr_t dma;
	size_t size;
};

struct fx_device {
	struct kref refs;
	struct device *device;
	struct clk *clock;
	void __iomem *registers;
	struct miscdevice misc;
	struct mutex lock;
	struct mutex buffer_lock;
	spinlock_t irq_lock;
	struct completion completion;
	struct fx_buffer source, destination;
	struct framefx_wait result;
	u32 expected_read, expected_written;
	u64 sequence, interrupts;
	int irq;
	bool owner, removed, quarantined, detached_idle;
	bool active, offline, pending;
};

static u32 fx_read(struct fx_device *device, unsigned int offset)
{
	return readl(device->registers + offset);
}

static void fx_write(struct fx_device *device, unsigned int offset, u32 value)
{
	writel(value, device->registers + offset);
}

static void fx_destroy(struct kref *refs)
{
	struct fx_device *device = container_of(refs, struct fx_device, refs);

	clk_disable_unprepare(device->clock);
	clk_put(device->clock);
	put_device(device->device);
	kfree(device);
}

static void fx_free(struct fx_device *device, struct fx_buffer *buffer)
{
	if (buffer->cpu)
		dma_free_coherent(device->device, buffer->size, buffer->cpu, buffer->dma);
	memset(buffer, 0, sizeof(*buffer));
}

static void fx_offline(struct fx_device *device)
{
	WRITE_ONCE(device->offline, true);
	complete_all(&device->completion);
}

static bool fx_alive(struct fx_device *device)
{
	if (fx_read(device, 0) == FX_MAGIC)
		return true;
	fx_offline(device);
	return false;
}

static bool fx_idle(struct fx_device *device)
{
	u32 state;

	if (device->removed || !fx_alive(device))
		return false;
	state = fx_read(device, FX_STATUS);
	if (state & ~7U) {
		fx_offline(device);
		return false;
	}
	return !(state & 1);
}

static bool fx_quiesce(struct fx_device *device)
{
	unsigned long deadline = jiffies + msecs_to_jiffies(FRAMEFX_MAX_WAIT_MS);

	do {
		if (fx_idle(device)) {
			fx_write(device, FX_IRQ_ENABLE, 0);
			synchronize_irq(device->irq);
			return fx_alive(device);
		}
		if (READ_ONCE(device->offline) || device->removed)
			break;
		msleep(10);
	} while (time_before(jiffies, deadline));
	return false;
}

static void fx_quarantine(struct fx_device *device)
{
	if (!device->quarantined) {
		device->quarantined = true;
		WRITE_ONCE(device->offline, true);
		kref_get(&device->refs);
		__module_get(THIS_MODULE);
		dev_err(device->device, "FRAMEFX_QUARANTINE: DMA idle unproven; retaining buffers and module until system teardown\n");
	}
}

static irqreturn_t fx_interrupt(int irq, void *opaque)
{
	struct fx_device *device = opaque;
	unsigned long flags;
	u32 pending, state, low;
	bool finished = false;

	spin_lock_irqsave(&device->irq_lock, flags);
	pending = fx_read(device, FX_IRQ_STATUS);
	if (!pending) {
		spin_unlock_irqrestore(&device->irq_lock, flags);
		return IRQ_NONE;
	}
	if (pending & ~3U) {
		goto invalid;
	} else {
		device->interrupts++;
		state = fx_read(device, FX_STATUS);
		if ((state & ~6U) || !(state & 2))
			goto invalid;
		if (device->active) {
			device->result.status = state;
			device->result.error_code = fx_read(device, 0x044);
			low = fx_read(device, 0x048);
			device->result.cycles = low | (u64)fx_read(device, 0x04c) << 32;
			device->result.bytes_read = fx_read(device, 0x050);
			device->result.bytes_written = fx_read(device, 0x054);
			device->result.irq_count = device->interrupts;
			if (device->result.error_code > 5 ||
			    !!device->result.error_code != !!(state & 4) ||
			    pending != (device->result.error_code ? 2U : 1U) ||
			    device->result.bytes_read > device->expected_read ||
			    device->result.bytes_written > device->expected_written ||
			    (!device->result.error_code &&
			     (device->result.bytes_read != device->expected_read ||
			      device->result.bytes_written != device->expected_written)) || !fx_alive(device))
				goto invalid;
			dma_rmb();
			device->active = false;
			finished = true;
		}
		fx_write(device, FX_IRQ_STATUS, pending & 3);
	}
	if (finished)
		complete_all(&device->completion);
	spin_unlock_irqrestore(&device->irq_lock, flags);
	return IRQ_HANDLED;
invalid:
	disable_irq_nosync(device->irq);
	fx_offline(device);
	spin_unlock_irqrestore(&device->irq_lock, flags);
	return IRQ_HANDLED;
}

static int fx_open(struct inode *inode, struct file *file)
{
	struct fx_device *device = container_of(file->private_data, struct fx_device, misc);
	int result = 0;

	mutex_lock(&device->lock);
	if (device->removed || READ_ONCE(device->offline))
		result = -ENODEV;
	else if (device->owner)
		result = -EBUSY;
	else {
		device->owner = true;
		device->result.job_id = 0;
		kref_get(&device->refs);
		file->private_data = device;
	}
	mutex_unlock(&device->lock);
	return result;
}

static int fx_release(struct inode *inode, struct file *file)
{
	struct fx_device *device = file->private_data;

	mutex_lock(&device->lock);
	if (device->source.cpu || device->destination.cpu) {
		if (device->removed ? device->detached_idle : fx_quiesce(device)) {
			fx_free(device, &device->source);
			fx_free(device, &device->destination);
			device->active = false;
			device->pending = false;
		} else {
			fx_quarantine(device);
		}
	}
	device->owner = false;
	mutex_unlock(&device->lock);
	kref_put(&device->refs, fx_destroy);
	return 0;
}

static int fx_allocate(struct fx_device *device, void __user *argument)
{
	struct framefx_buffers request;
	struct fx_buffer source = {}, destination = {};

	if (copy_from_user(&request, argument, sizeof(request)))
		return -EFAULT;
	if (device->source.cpu || device->destination.cpu)
		return -EBUSY;
	if (!request.source_bytes || !request.destination_bytes ||
	    request.source_bytes > FRAMEFX_MAX_BUFFER || request.destination_bytes > FRAMEFX_MAX_BUFFER ||
	    request.reserved[0] || request.reserved[1] || request.source_offset || request.destination_offset)
		return -EINVAL;
	source.size = PAGE_ALIGN(request.source_bytes);
	destination.size = PAGE_ALIGN(request.destination_bytes);
	source.cpu = dma_alloc_coherent(device->device, source.size, &source.dma, GFP_KERNEL);
	destination.cpu = dma_alloc_coherent(device->device, destination.size, &destination.dma, GFP_KERNEL);
	if (!source.cpu || !destination.cpu) {
		fx_free(device, &source);
		fx_free(device, &destination);
		return -ENOMEM;
	}
	memset(source.cpu, 0, source.size);
	memset(destination.cpu, 0, destination.size);
	request.source_bytes = source.size;
	request.destination_bytes = destination.size;
	request.source_offset = 0;
	request.destination_offset = FRAMEFX_DESTINATION_OFFSET;
	if (copy_to_user(argument, &request, sizeof(request))) {
		fx_free(device, &source);
		fx_free(device, &destination);
		return -EFAULT;
	}
	mutex_lock(&device->buffer_lock);
	device->source = source;
	device->destination = destination;
	mutex_unlock(&device->buffer_lock);
	return 0;
}

static int fx_submit(struct fx_device *device, void __user *argument)
{
	struct framefx_submit request;
	unsigned long flags;

	if (copy_from_user(&request, argument, sizeof(request)))
		return -EFAULT;
	if (device->pending || READ_ONCE(device->active))
		return -EBUSY;
	if (!device->source.cpu || !device->destination.cpu || request.reserved[0] || request.reserved[1] || request.job_id ||
	    !request.width || request.width > 1024 || !request.height || request.height > 1024 ||
	    request.mode > 3 || request.threshold > 255 || (request.mode == 3 && (request.width < 3 || request.height < 3)) ||
	    request.source_stride % 64 || request.destination_stride % 64 ||
	    request.source_stride < ALIGN(request.width, 64) || request.destination_stride < ALIGN(request.width, 64) ||
	    (u64)request.source_stride * request.height > device->source.size ||
	    (u64)request.destination_stride * request.height > device->destination.size)
		return -EINVAL;
	if (!fx_idle(device))
		return -EIO;
	request.job_id = device->sequence + 1;
	if (!request.job_id)
		return -EOVERFLOW;
	if (copy_to_user(argument, &request, sizeof(request)))
		return -EFAULT;
	fx_write(device, FX_IRQ_STATUS, 3);
	fx_write(device, 0x014, lower_32_bits(device->source.dma));
	fx_write(device, 0x018, upper_32_bits(device->source.dma));
	fx_write(device, 0x01c, lower_32_bits(device->destination.dma));
	fx_write(device, 0x020, upper_32_bits(device->destination.dma));
	fx_write(device, 0x024, request.width);
	fx_write(device, 0x028, request.height);
	fx_write(device, 0x02c, request.source_stride);
	fx_write(device, 0x030, request.destination_stride);
	fx_write(device, 0x034, request.mode);
	fx_write(device, 0x038, request.threshold);
	if (!fx_alive(device))
		return -EIO;
	spin_lock_irqsave(&device->irq_lock, flags);
	device->sequence = request.job_id;
	memset(&device->result, 0, sizeof(device->result));
	device->result.job_id = request.job_id;
	device->pending = true;
	device->active = true;
	device->expected_read = ALIGN(request.width, 8) * request.height;
	device->expected_written = request.width * request.height;
	reinit_completion(&device->completion);
	spin_unlock_irqrestore(&device->irq_lock, flags);
	dma_wmb();
	fx_write(device, FX_IRQ_ENABLE, 3);
	fx_write(device, FX_CONTROL, 1);
	return fx_alive(device) && !READ_ONCE(device->offline) ? 0 : -EIO;
}

static int fx_wait(struct fx_device *device, void __user *argument)
{
	struct framefx_wait request;
	unsigned long flags;
	long waited;

	if (copy_from_user(&request, argument, sizeof(request)))
		return -EFAULT;
	if (!request.timeout_ms || request.timeout_ms > FRAMEFX_MAX_WAIT_MS || request.reserved)
		return -EINVAL;
	if (!request.job_id || request.job_id != device->result.job_id)
		return -ESTALE;
	waited = wait_for_completion_interruptible_timeout(&device->completion, msecs_to_jiffies(request.timeout_ms));
	if (waited < 0)
		return waited;
	if (READ_ONCE(device->offline) || !fx_alive(device)) {
		fx_offline(device);
		return -EIO;
	}
	if (!waited)
		return -ETIMEDOUT;
	spin_lock_irqsave(&device->irq_lock, flags);
	request = device->result;
	spin_unlock_irqrestore(&device->irq_lock, flags);
	if (copy_to_user(argument, &request, sizeof(request)))
		return -EFAULT;
	device->pending = false;
	return request.error_code ? -EIO : 0;
}

static long fx_ioctl(struct file *file, unsigned int command, unsigned long value)
{
	struct fx_device *device = file->private_data;
	void __user *argument = (void __user *)value;
	struct framefx_caps caps = { FRAMEFX_ABI_VERSION, 0, 0, 1024, 1024, 64, FRAMEFX_MAX_BUFFER, 0 };
	int result;

	if (mutex_lock_interruptible(&device->lock))
		return -ERESTARTSYS;
	if (device->removed || READ_ONCE(device->offline)) {
		result = -ENODEV;
		goto out;
	}
	switch (command) {
	case FRAMEFX_QUERY_CAPS:
		caps.hardware_version = fx_read(device, 0x004);
		caps.capabilities = fx_read(device, 0x008);
		if (caps.hardware_version != 0x00010000 || caps.capabilities != 0x10f || !fx_alive(device)) {
			fx_offline(device);
			result = -EIO;
			break;
		}
		result = copy_to_user(argument, &caps, sizeof(caps)) ? -EFAULT : 0;
		break;
	case FRAMEFX_ALLOC_BUFS:
		result = fx_allocate(device, argument);
		break;
	case FRAMEFX_SUBMIT:
		result = fx_submit(device, argument);
		break;
	case FRAMEFX_WAIT:
		result = fx_wait(device, argument);
		break;
	case FRAMEFX_RESET:
		if (value)
			result = -EINVAL;
		else if (READ_ONCE(device->active))
			result = -EBUSY;
		else if (!fx_idle(device))
			result = READ_ONCE(device->offline) ? -EIO : -EBUSY;
		else {
			fx_write(device, FX_CONTROL, 2);
			if (!fx_alive(device)) {
				result = -EIO;
				break;
			}
			device->pending = false;
			device->result.job_id = 0;
			result = 0;
		}
		break;
	default:
		result = -ENOTTY;
	}
out:
	mutex_unlock(&device->lock);
	return result;
}

static int fx_mmap(struct file *file, struct vm_area_struct *area)
{
	struct fx_device *device = file->private_data;
	struct fx_buffer *buffer;
	int result = -EINVAL;

	mutex_lock(&device->buffer_lock);
	if (READ_ONCE(device->removed) || READ_ONCE(device->offline)) {
		result = -ENODEV;
		goto out;
	}
	if (area->vm_pgoff == 0)
		buffer = &device->source;
	else if (area->vm_pgoff == FRAMEFX_DESTINATION_OFFSET >> PAGE_SHIFT)
		buffer = &device->destination;
	else
		goto out;
	if (!buffer->cpu || !(area->vm_flags & VM_SHARED) || area->vm_flags & VM_EXEC ||
	    area->vm_end - area->vm_start != buffer->size)
		goto out;
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 3, 0)
	vm_flags_clear(area, VM_MAYEXEC);
	vm_flags_set(area, VM_DONTCOPY | VM_DONTEXPAND | VM_DONTDUMP);
#else
	area->vm_flags &= ~VM_MAYEXEC;
	area->vm_flags |= VM_DONTCOPY | VM_DONTEXPAND | VM_DONTDUMP;
#endif
	area->vm_pgoff = 0;
	result = dma_mmap_coherent(device->device, area, buffer->cpu, buffer->dma, buffer->size);
out:
	mutex_unlock(&device->buffer_lock);
	return result;
}

static const struct file_operations fx_operations = {
	.owner = THIS_MODULE,
	.open = fx_open,
	.release = fx_release,
	.unlocked_ioctl = fx_ioctl,
	.compat_ioctl = compat_ptr_ioctl,
	.mmap = fx_mmap,
};

static int fx_probe(struct platform_device *platform)
{
	struct fx_device *device;
	int result;

	if (of_property_read_bool(platform->dev.of_node, "example,concurrency-spike"))
		return -ENODEV;
	device = kzalloc(sizeof(*device), GFP_KERNEL);
	if (!device)
		return -ENOMEM;
	kref_init(&device->refs);
	device->device = get_device(&platform->dev);
	mutex_init(&device->lock);
	mutex_init(&device->buffer_lock);
	spin_lock_init(&device->irq_lock);
	init_completion(&device->completion);
	device->clock = clk_get_optional(&platform->dev, NULL);
	if (IS_ERR(device->clock)) {
		result = PTR_ERR(device->clock);
		device->clock = NULL;
		goto failed;
	}
	result = clk_prepare_enable(device->clock);
	if (result) {
		clk_put(device->clock);
		device->clock = NULL;
		goto failed;
	}
	device->registers = devm_platform_ioremap_resource(platform, 0);
	if (IS_ERR(device->registers)) {
		result = PTR_ERR(device->registers);
		goto failed;
	}
	if (fx_read(device, 0) != FX_MAGIC || fx_read(device, 4) != 0x00010000 || !fx_idle(device)) {
		result = -ENODEV;
		goto failed;
	}
	result = dma_set_mask_and_coherent(&platform->dev, DMA_BIT_MASK(64));
	if (result)
		result = dma_set_mask_and_coherent(&platform->dev, DMA_BIT_MASK(32));
	if (result)
		goto failed;
	device->irq = platform_get_irq(platform, 0);
	if (device->irq < 0) {
		result = device->irq;
		goto failed;
	}
	fx_write(device, FX_IRQ_ENABLE, 0);
	fx_write(device, FX_IRQ_STATUS, 3);
	result = request_irq(device->irq, fx_interrupt, 0, "framefx", device);
	if (result)
		goto failed;
	device->misc.minor = MISC_DYNAMIC_MINOR;
	device->misc.name = "framefx0";
	device->misc.fops = &fx_operations;
	device->misc.parent = &platform->dev;
	device->misc.mode = 0600;
	result = misc_register(&device->misc);
	if (result) {
		free_irq(device->irq, device);
		goto failed;
	}
	platform_set_drvdata(platform, device);
	dev_info(&platform->dev, "FRAMEFX_READY ABI=1 IRQ=%d\n", device->irq);
	return 0;
failed:
	kref_put(&device->refs, fx_destroy);
	return result;
}

#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 11, 0)
static void fx_remove(struct platform_device *platform)
#else
static int fx_remove(struct platform_device *platform)
#endif
{
	struct fx_device *device = platform_get_drvdata(platform);

	misc_deregister(&device->misc);
	mutex_lock(&device->lock);
	device->detached_idle = fx_quiesce(device);
	if (!device->detached_idle && (device->source.cpu || device->destination.cpu))
		fx_quarantine(device);
	WRITE_ONCE(device->removed, true);
	WRITE_ONCE(device->offline, true);
	complete_all(&device->completion);
	free_irq(device->irq, device);
	mutex_unlock(&device->lock);
	kref_put(&device->refs, fx_destroy);
#if LINUX_VERSION_CODE < KERNEL_VERSION(6, 11, 0)
	return 0;
#endif
}

static const struct of_device_id fx_match[] = {
	{ .compatible = "example,axi-framefx-1.0" },
	{}
};
MODULE_DEVICE_TABLE(of, fx_match);

static struct platform_driver fx_driver = {
	.probe = fx_probe,
	.remove = fx_remove,
	.driver = {
		.name = "framefx",
		.of_match_table = fx_match,
		.suppress_bind_attrs = true,
	},
};
module_platform_driver(fx_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Framefx ABI 1 coherent-buffer accelerator driver");
