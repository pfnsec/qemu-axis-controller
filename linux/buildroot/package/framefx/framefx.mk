FRAMEFX_VERSION = 1
FRAMEFX_SITE = $(BR2_EXTERNAL_AXI_LAB_PATH)/..
FRAMEFX_SITE_METHOD = local
FRAMEFX_LICENSE = GPL-2.0
FRAMEFX_MODULE_SUBDIRS = framefx/driver

define FRAMEFX_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) -std=c11 -Wall -Wextra -Werror -I$(@D)/uapi $(@D)/framefx/app/framefx-probe.c -o $(@D)/framefx-probe
	$(TARGET_CC) $(TARGET_CFLAGS) -std=c11 -Wall -Wextra -Werror -I$(@D)/uapi $(@D)/framefx/app/framefx-test.c -o $(@D)/framefx-test
	$(TARGET_CC) $(TARGET_CFLAGS) -std=c11 -Wall -Wextra -Werror -I$(@D)/uapi $(@D)/framefx/app/framefx-lifecycle.c -o $(@D)/framefx-lifecycle
	$(TARGET_CC) $(TARGET_CFLAGS) -std=c11 -Wall -Wextra -Werror -pthread -I$(@D)/uapi $(@D)/framefx/app/framefx-stress.c -o $(@D)/framefx-stress
	$(TARGET_CC) $(TARGET_CFLAGS) -std=c11 -Wall -Wextra -Werror -pthread -I$(@D)/uapi $(@D)/framefx/app/framefx-bench.c -o $(@D)/framefx-bench
	$(TARGET_CC) $(TARGET_CFLAGS) -std=c11 -Wall -Wextra -Werror -I$(@D)/uapi $(@D)/framefx/app/framefx-control-test.c -o $(@D)/framefx-control-test
endef

define FRAMEFX_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/framefx-probe $(TARGET_DIR)/usr/bin/framefx-probe
	$(INSTALL) -D -m 0755 $(@D)/framefx-test $(TARGET_DIR)/usr/bin/framefx-test
	$(INSTALL) -D -m 0755 $(@D)/framefx-lifecycle $(TARGET_DIR)/usr/bin/framefx-lifecycle
	$(INSTALL) -D -m 0755 $(@D)/framefx-stress $(TARGET_DIR)/usr/bin/framefx-stress
	$(INSTALL) -D -m 0755 $(@D)/framefx-bench $(TARGET_DIR)/usr/bin/framefx-bench
	$(INSTALL) -D -m 0755 $(@D)/framefx-control-test $(TARGET_DIR)/usr/bin/framefx-control-test
endef

$(eval $(kernel-module))
$(eval $(generic-package))
