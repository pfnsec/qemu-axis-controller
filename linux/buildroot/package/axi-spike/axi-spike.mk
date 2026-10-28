AXI_SPIKE_VERSION = 1
AXI_SPIKE_SITE = $(BR2_EXTERNAL_AXI_LAB_PATH)/../spike
AXI_SPIKE_SITE_METHOD = local
AXI_SPIKE_LICENSE = GPL-2.0

$(eval $(kernel-module))
$(eval $(generic-package))
