// SPDX-License-Identifier: MIT
//
// /init of Cherry's initramfs. The whole OS is one EFI binary (a UKI) whose
// initramfs contains this program and rootfs.squashfs. Mount that image
// read-only through a loop device, make it the root filesystem and start
// systemd from it. /var is a tmpfs, set up by systemd (Buildroot's var.mount).
//
// Any failure is fatal: exiting makes the kernel panic, and panic=N reboots.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/loop.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

#define IMAGE "/rootfs.squashfs"
#define NEWROOT "/newroot"
#define INIT "/sbin/init"

static void msg(const char *fmt, ...)
{
	char buf[256];
	va_list ap;
	int len, fd;

	len = snprintf(buf, sizeof(buf), "cherry-init: ");
	va_start(ap, fmt);
	len += vsnprintf(buf + len, sizeof(buf) - len, fmt, ap);
	va_end(ap);
	if (len >= (int)sizeof(buf) - 1)
		len = sizeof(buf) - 2;
	buf[len++] = '\n';

	fd = open("/dev/kmsg", O_WRONLY | O_CLOEXEC);
	if (fd < 0)
		fd = dup(STDERR_FILENO);
	if (fd >= 0) {
		if (write(fd, buf, len) < 0) {
			/* nothing left to report to */
		}
		close(fd);
	}
}

static void die(const char *what)
{
	msg("%s: %s", what, strerror(errno));
	exit(1);
}

int main(int argc, char *argv[])
{
	struct loop_config config;
	char loopdev[32];
	int image, control, loop, n;

	(void)argc;

	if (mount("devtmpfs", "/dev", "devtmpfs", MS_NOSUID, "mode=0755") < 0 && errno != EBUSY)
		die("mounting /dev");

	image = open(IMAGE, O_RDONLY | O_CLOEXEC);
	if (image < 0)
		die("opening " IMAGE);

	control = open("/dev/loop-control", O_RDWR | O_CLOEXEC);
	if (control < 0)
		die("opening /dev/loop-control");
	n = ioctl(control, LOOP_CTL_GET_FREE);
	if (n < 0)
		die("allocating a loop device");
	close(control);

	snprintf(loopdev, sizeof(loopdev), "/dev/loop%d", n);
	loop = open(loopdev, O_RDONLY | O_CLOEXEC);
	if (loop < 0)
		die("opening the loop device");

	memset(&config, 0, sizeof(config));
	config.fd = image;
	config.info.lo_flags = LO_FLAGS_READ_ONLY | LO_FLAGS_AUTOCLEAR;
	strncpy((char *)config.info.lo_file_name, IMAGE, LO_NAME_SIZE - 1);
	if (ioctl(loop, LOOP_CONFIGURE, &config) < 0)
		die("configuring the loop device");

	if (mkdir(NEWROOT, 0755) < 0 && errno != EEXIST)
		die("creating " NEWROOT);
	if (mount(loopdev, NEWROOT, "squashfs", MS_RDONLY, NULL) < 0)
		die("mounting the root filesystem");
	close(loop);
	close(image);

	if (mount("/dev", NEWROOT "/dev", NULL, MS_MOVE, NULL) < 0)
		die("moving /dev");
	if (chdir(NEWROOT) < 0)
		die("entering " NEWROOT);
	if (mount(".", "/", NULL, MS_MOVE, NULL) < 0)
		die("moving the root filesystem");
	if (chroot(".") < 0 || chdir("/") < 0)
		die("switching root");

	msg("starting " INIT " from the read-only root filesystem");
	argv[0] = INIT;
	execv(INIT, argv);
	die("starting " INIT);
	return 1;
}
