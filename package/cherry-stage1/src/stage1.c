/*
 * Cherry's stage 1, the /init of the initramfs. The initramfs holds only this
 * program and the root filesystem, as a compressed EROFS image. Mount the image
 * read-only, straight from the file, make it the root, and run its /init.
 *
 * The image stays in the initramfs (a ramfs: rootfstype=ramfs), now hidden
 * under the new root: its pages are the root filesystem's only copy,
 * compressed. The kernel caches the files in use, decompressed, and drops
 * that cache under memory pressure.
 */
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <unistd.h>

static void fail(const char *what)
{
	/* PID 1 exits: the kernel panics, and panic=10 reboots (and HTTP boots). */
	fprintf(stderr, "cherry-stage1: %s: %s\n", what, strerror(errno));
	_exit(1);
}

int main(int argc, char *argv[])
{
	(void)argc;
	if (mount("/rootfs.erofs", "/sysroot", "erofs", MS_RDONLY, NULL))
		fail("mount /rootfs.erofs on /sysroot");
	/* What switch_root does, minus emptying the initramfs: the image stays. */
	if (chdir("/sysroot"))
		fail("chdir /sysroot");
	if (mount(".", "/", NULL, MS_MOVE, NULL))
		fail("move /sysroot to /");
	if (chroot("."))
		fail("chroot");
	if (chdir("/"))
		fail("chdir /");
	execv("/init", argv);
	fail("exec /init");
}
