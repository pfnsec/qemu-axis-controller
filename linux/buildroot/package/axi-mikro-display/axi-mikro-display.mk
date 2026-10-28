AXI_MIKRO_DISPLAY_VERSION = 1
AXI_MIKRO_DISPLAY_SITE = $(BR2_EXTERNAL_AXI_LAB_PATH)/../hardware/ultra96-mikro
AXI_MIKRO_DISPLAY_SITE_METHOD = local
AXI_MIKRO_DISPLAY_LICENSE = UNKNOWN

define AXI_MIKRO_DISPLAY_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) -std=c11 -Wall -Wextra -Werror $(@D)/display.c -o $(@D)/axi-lab-display
endef

define AXI_MIKRO_DISPLAY_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/axi-lab-display $(TARGET_DIR)/usr/bin/axi-lab-display
endef

$(eval $(generic-package))
