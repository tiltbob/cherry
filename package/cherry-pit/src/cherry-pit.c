/*
 * cherry-pit, Cherry's stage 1: the /init of the initramfs. The initramfs holds
 * only this program and the root filesystem, as a compressed EROFS image, and
 * what systemd-stub added to it. Mount the image read-only, straight from the
 * file, give it its /run, make it the root, and run its /init.
 *
 * The image stays in the initramfs (a ramfs: rootfstype=ramfs), now hidden
 * under the new root: its pages are the root filesystem's only copy,
 * compressed. The kernel caches the files in use, decompressed, and drops
 * that cache under memory pressure.
 *
 * /run is a tmpfs mounted here, with the options systemd would give it, so
 * that the UKI's PCR signature and public key, which systemd-stub put in the
 * initramfs as /.extra/tpm2-pcr-signature.json and tpm2-pcr-public-key.pem
 * (its .pcrsig and .pcrpkey sections), can go to /run/systemd, where systemd's
 * tools look for them, before the initramfs is out of reach: they seal the
 * disks' keys to the boot state (/usr/lib/cherry/var, scripts/pcrsign.py).
 */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

static void fail(const char *what)
{
	/* PID 1 exits: the kernel panics, and panic=10 reboots (and HTTP boots). */
	fprintf(stderr, "cherry-pit: %s: %s\n", what, strerror(errno));
	_exit(1);
}

/* A file the stub left, if it did, copied readable by all, as the stub left it. */
static void copy(const char *from, const char *to)
{
	char buf[4096];
	ssize_t n, done, w;
	int in, out;

	in = open(from, O_RDONLY);
	if (in < 0) {
		if (errno == ENOENT)
			return;
		fail(from);
	}
	out = open(to, O_WRONLY | O_CREAT | O_EXCL, 0444);
	if (out < 0)
		fail(to);
	while ((n = read(in, buf, sizeof buf)) > 0)
		for (done = 0; done < n; done += w)
			if ((w = write(out, buf + done, n - done)) < 0)
				fail(to);
	if (n < 0)
		fail(from);
	close(in);
	close(out);
}

int main(int argc, char *argv[])
{
	(void)argc;
	if (mount("/rootfs.erofs", "/sysroot", "erofs", MS_RDONLY, NULL))
		fail("mount /rootfs.erofs on /sysroot");
	if (mount("tmpfs", "/sysroot/run", "tmpfs", MS_NOSUID | MS_NODEV | MS_STRICTATIME,
		  "mode=0755,size=20%,nr_inodes=800k"))
		fail("mount tmpfs on /sysroot/run");
	if (mkdir("/sysroot/run/systemd", 0755))
		fail("mkdir /sysroot/run/systemd");
	copy("/.extra/tpm2-pcr-signature.json", "/sysroot/run/systemd/tpm2-pcr-signature.json");
	copy("/.extra/tpm2-pcr-public-key.pem", "/sysroot/run/systemd/tpm2-pcr-public-key.pem");
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
