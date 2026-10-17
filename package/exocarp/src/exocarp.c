/*
 * exocarp: sshd's host key in the TPM. An ssh-agent for one key, the TPM's
 * primary ECC NIST P-256 signing key in its endorsement hierarchy, made from a
 * fixed template: the TPM derives it from its endorsement seed, which survives
 * tpm2_clear, so every boot gets the same key with nothing stored anywhere,
 * and the private key never leaves the TPM. sshd's HostKeyAgent= names the
 * socket this serves, and its HostKey= the public key this writes (-p).
 *
 *   exocarp [-T tcti] -p FILE    write the public key, in OpenSSH's format, to FILE
 *   exocarp [-T tcti] [-l PATH]  serve the agent protocol on the socket systemd
 *                                passed (exocarp.socket), or else on PATH
 *
 * tcti names the TPM as tpm2-tools' -T does: device:/dev/tpmrm0, the kernel's
 * resource manager, by default; swtpm:path=... for a swtpm (tests/exocarp.sh).
 *
 * The key is the one tpm2_createprimary shows for
 *   -C e -G ecc256:ecdsa-sha256 -g sha256 \
 *   -a 'fixedtpm|fixedparent|sensitivedataorigin|userwithauth|sign|noda'
 * with no unique data, created again here at every start, which takes a TPM
 * command, not a secret.
 *
 * The protocol is ssh-agent's (draft-miller-ssh-agent): a connection sends
 * requests, each a 4-byte big-endian length then a type byte and the body.
 * sshd keeps one connection per session and sends SSH_AGENTC_SIGN_REQUEST for
 * the key's blob and the data to sign, once per connection; the TPM signs
 * SHA-256 of the data and the reply carries the ECDSA signature in SSH's
 * format. SSH_AGENTC_REQUEST_IDENTITIES lists the key (ssh-add -L shows it).
 * Anything else, or a request for another key, gets SSH_AGENT_FAILURE.
 * Connections are served from this one process, in turn, as requests come.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <openssl/evp.h>
#include <tss2/tss2_esys.h>
#include <tss2/tss2_rc.h>
#include <tss2/tss2_tctildr.h>

#define SSH_AGENT_FAILURE 5
#define SSH_AGENTC_REQUEST_IDENTITIES 11
#define SSH_AGENT_IDENTITIES_ANSWER 12
#define SSH_AGENTC_SIGN_REQUEST 13
#define SSH_AGENT_SIGN_RESPONSE 14

#define KEY_TYPE "ecdsa-sha2-nistp256"
#define CURVE "nistp256"
#define COORDINATE 32 /* bytes of a P-256 coordinate */
#define COMMENT "exocarp"
/* The protocol's limit on a message; sshd's requests are a few hundred bytes. */
#define MAX_MESSAGE (256 * 1024)
#define MAX_CLIENTS 1024

static const char *tcti_conf = "device:/dev/tpmrm0";
static TSS2_TCTI_CONTEXT *tcti;
static ESYS_CONTEXT *esys;
static volatile sig_atomic_t stopping;
static ESYS_TR key = ESYS_TR_NONE;
/* The key as SSH encodes it: string type, string curve, string point. */
static uint8_t key_blob[4 + sizeof KEY_TYPE + 4 + sizeof CURVE + 4 + 1 + 2 * COORDINATE];
static size_t key_blob_len;

static void logmsg(const char *fmt, ...)
{
	va_list ap;

	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	fputc('\n', stderr);
}

static void fatal(const char *fmt, ...)
{
	va_list ap;

	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	fputc('\n', stderr);
	exit(1);
}

/* A growing byte buffer, for SSH's wire encoding. */
struct buf {
	uint8_t *data;
	size_t len, cap;
};

static void buf_put(struct buf *b, const void *p, size_t n)
{
	if (b->len + n > b->cap) {
		size_t cap = b->cap ? b->cap : 256;

		while (cap < b->len + n)
			cap *= 2;
		b->data = realloc(b->data, cap);
		if (!b->data)
			fatal("out of memory");
		b->cap = cap;
	}
	memcpy(b->data + b->len, p, n);
	b->len += n;
}

static void buf_byte(struct buf *b, uint8_t v)
{
	buf_put(b, &v, 1);
}

static void buf_u32(struct buf *b, uint32_t v)
{
	uint8_t be[4] = { v >> 24, v >> 16, v >> 8, v };

	buf_put(b, be, 4);
}

static void buf_string(struct buf *b, const void *p, size_t n)
{
	buf_u32(b, n);
	buf_put(b, p, n);
}

/* An unsigned big-endian integer as SSH's mpint: no leading zeros, and a zero
 * byte ahead of a set high bit. */
static void buf_mpint(struct buf *b, const uint8_t *p, size_t n)
{
	while (n > 0 && *p == 0)
		p++, n--;
	buf_u32(b, n + (n > 0 && (*p & 0x80)));
	if (n > 0 && (*p & 0x80))
		buf_byte(b, 0);
	buf_put(b, p, n);
}

/* A cursor over a received message. */
struct cursor {
	const uint8_t *p;
	size_t n;
};

static int get_u32(struct cursor *c, uint32_t *v)
{
	if (c->n < 4)
		return 0;
	*v = (uint32_t)c->p[0] << 24 | (uint32_t)c->p[1] << 16 | (uint32_t)c->p[2] << 8 | c->p[3];
	c->p += 4;
	c->n -= 4;
	return 1;
}

static int get_string(struct cursor *c, const uint8_t **p, size_t *n)
{
	uint32_t len;

	if (!get_u32(c, &len) || len > c->n)
		return 0;
	*p = c->p;
	*n = len;
	c->p += len;
	c->n -= len;
	return 1;
}

static void tpm_open(void)
{
	TSS2_RC rc;

	rc = Tss2_TctiLdr_Initialize(tcti_conf, &tcti);
	if (rc)
		fatal("cannot open the TPM, %s: %s", tcti_conf, Tss2_RC_Decode(rc));
	rc = Esys_Initialize(&esys, tcti, NULL);
	if (rc)
		fatal("cannot start a TPM session: %s", Tss2_RC_Decode(rc));
}

/* The key out of the TPM's memory. The kernel's resource manager does that
 * when the process exits; a TPM reached directly, such as a swtpm, keeps
 * transient keys until told. */
static void tpm_close(void)
{
	if (key != ESYS_TR_NONE)
		Esys_FlushContext(esys, key);
	Esys_Finalize(&esys);
	Tss2_TctiLdr_Finalize(&tcti);
}

/* The host key: the endorsement hierarchy's primary key for the template in
 * the header. Fills key and key_blob. */
static TSS2_RC key_create(void)
{
	TPM2B_SENSITIVE_CREATE sensitive = { .size = 0 };
	TPM2B_PUBLIC template = {
		.publicArea = {
			.type = TPM2_ALG_ECC,
			.nameAlg = TPM2_ALG_SHA256,
			.objectAttributes = TPMA_OBJECT_FIXEDTPM | TPMA_OBJECT_FIXEDPARENT |
				TPMA_OBJECT_SENSITIVEDATAORIGIN | TPMA_OBJECT_USERWITHAUTH |
				TPMA_OBJECT_SIGN_ENCRYPT | TPMA_OBJECT_NODA,
			.parameters.eccDetail = {
				.symmetric.algorithm = TPM2_ALG_NULL,
				.scheme = {
					.scheme = TPM2_ALG_ECDSA,
					.details.ecdsa.hashAlg = TPM2_ALG_SHA256,
				},
				.curveID = TPM2_ECC_NIST_P256,
				.kdf.scheme = TPM2_ALG_NULL,
			},
		},
	};
	TPM2B_DATA outside = { .size = 0 };
	TPML_PCR_SELECTION pcrs = { .count = 0 };
	TPM2B_PUBLIC *pub = NULL;
	TPMS_ECC_POINT *q;
	uint8_t point[1 + 2 * COORDINATE] = { 4 }; /* uncompressed: 04 || X || Y */
	struct buf b = { key_blob, 0, sizeof key_blob };
	TSS2_RC rc;

	if (key != ESYS_TR_NONE) {
		Esys_FlushContext(esys, key);
		key = ESYS_TR_NONE;
	}
	rc = Esys_CreatePrimary(esys, ESYS_TR_RH_ENDORSEMENT, ESYS_TR_PASSWORD, ESYS_TR_NONE, ESYS_TR_NONE,
				&sensitive, &template, &outside, &pcrs, &key, &pub, NULL, NULL, NULL);
	if (rc)
		return rc;
	q = &pub->publicArea.unique.ecc;
	if (q->x.size > COORDINATE || q->y.size > COORDINATE)
		fatal("the TPM's key is not a P-256 key");
	/* Left-padded to the coordinate size, should the TPM shorten them. */
	memcpy(point + 1 + COORDINATE - q->x.size, q->x.buffer, q->x.size);
	memcpy(point + 1 + 2 * COORDINATE - q->y.size, q->y.buffer, q->y.size);
	Esys_Free(pub);
	/* key_blob is exactly as large as this; buf_put never grows it. */
	buf_string(&b, KEY_TYPE, sizeof KEY_TYPE - 1);
	buf_string(&b, CURVE, sizeof CURVE - 1);
	buf_string(&b, point, sizeof point);
	key_blob_len = b.len;
	return 0;
}

/* The TPM's ECDSA signature over SHA-256 of the data, as SSH encodes it:
 * string type, string (mpint r, mpint s). */
static TSS2_RC tpm_sign(const uint8_t *data, size_t len, struct buf *out)
{
	TPM2B_DIGEST digest = { .size = 32 };
	TPMT_SIG_SCHEME scheme = {
		.scheme = TPM2_ALG_ECDSA,
		.details.ecdsa.hashAlg = TPM2_ALG_SHA256,
	};
	/* No ticket: the key is unrestricted, so it signs any digest. */
	TPMT_TK_HASHCHECK validation = {
		.tag = TPM2_ST_HASHCHECK,
		.hierarchy = TPM2_RH_NULL,
		.digest.size = 0,
	};
	TPMT_SIGNATURE *sig = NULL;
	struct buf rs = { 0 };
	unsigned int md_len;
	TSS2_RC rc;

	if (!EVP_Digest(data, len, digest.buffer, &md_len, EVP_sha256(), NULL) || md_len != 32)
		fatal("SHA-256 failed");
	rc = Esys_Sign(esys, key, ESYS_TR_PASSWORD, ESYS_TR_NONE, ESYS_TR_NONE, &digest, &scheme, &validation, &sig);
	if (rc)
		return rc;
	buf_mpint(&rs, sig->signature.ecdsa.signatureR.buffer, sig->signature.ecdsa.signatureR.size);
	buf_mpint(&rs, sig->signature.ecdsa.signatureS.buffer, sig->signature.ecdsa.signatureS.size);
	Esys_Free(sig);
	buf_string(out, KEY_TYPE, sizeof KEY_TYPE - 1);
	buf_string(out, rs.data, rs.len);
	free(rs.data);
	return 0;
}

/* The public key, in OpenSSH's one-line format, to the file: a new one, for
 * its owner alone, as sshd first tries a HostKey as a private key and warns
 * about one of its own that others can read. */
static void write_public_key(const char *path)
{
	uint8_t b64[2 * sizeof key_blob];
	FILE *f;
	int fd;
	TSS2_RC rc;

	rc = key_create();
	if (rc)
		fatal("the TPM cannot create the host key: %s", Tss2_RC_Decode(rc));
	EVP_EncodeBlock(b64, key_blob, key_blob_len);
	unlink(path);
	fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
	f = fd < 0 ? NULL : fdopen(fd, "w");
	if (!f || fprintf(f, "%s %s %s\n", KEY_TYPE, b64, COMMENT) < 0 || fclose(f))
		fatal("cannot write %s: %s", path, strerror(errno));
}

/* One request, and its reply in out. */
static void handle(uint8_t type, struct cursor body, struct buf *out)
{
	const uint8_t *blob, *data;
	size_t blob_len, data_len;
	uint32_t flags;
	struct buf sig = { 0 };
	TSS2_RC rc;

	switch (type) {
	case SSH_AGENTC_REQUEST_IDENTITIES:
		buf_byte(out, SSH_AGENT_IDENTITIES_ANSWER);
		buf_u32(out, 1);
		buf_string(out, key_blob, key_blob_len);
		buf_string(out, COMMENT, sizeof COMMENT - 1);
		return;
	case SSH_AGENTC_SIGN_REQUEST:
		if (!get_string(&body, &blob, &blob_len) || !get_string(&body, &data, &data_len) ||
		    !get_u32(&body, &flags)) {
			logmsg("malformed sign request");
			break;
		}
		if (blob_len != key_blob_len || memcmp(blob, key_blob, blob_len)) {
			logmsg("sign request for a key that is not the host key");
			break;
		}
		rc = tpm_sign(data, data_len, &sig);
		if (rc) {
			/* The TPM may have lost the key, e.g. after a reset: make it again, once. */
			logmsg("the TPM did not sign: %s; creating the key again", Tss2_RC_Decode(rc));
			rc = key_create();
			if (!rc)
				rc = tpm_sign(data, data_len, &sig);
		}
		if (rc) {
			logmsg("the TPM did not sign: %s", Tss2_RC_Decode(rc));
			break;
		}
		buf_byte(out, SSH_AGENT_SIGN_RESPONSE);
		buf_string(out, sig.data, sig.len);
		free(sig.data);
		return;
	default:
		logmsg("request of type %u is not supported", type);
		break;
	}
	out->len = 0;
	buf_byte(out, SSH_AGENT_FAILURE);
}

static int write_all(int fd, const uint8_t *p, size_t n)
{
	while (n > 0) {
		ssize_t w = write(fd, p, n);

		if (w < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		p += w;
		n -= w;
	}
	return 0;
}

struct client {
	int fd;
	struct buf in; /* what arrived and is not yet a whole request */
};

/* What arrived on the connection: every whole request in it is answered.
 * Returns 0 to keep the connection, -1 to close it. */
static int serve(struct client *c)
{
	uint8_t chunk[4096];
	ssize_t r;

	r = read(c->fd, chunk, sizeof chunk);
	if (r < 0)
		return errno == EINTR || errno == EAGAIN ? 0 : -1;
	if (r == 0)
		return -1;
	buf_put(&c->in, chunk, r);
	for (;;) {
		struct cursor cur = { c->in.data, c->in.len };
		struct buf reply = { 0 }, framed = { 0 };
		uint32_t len;
		int rc;

		if (!get_u32(&cur, &len))
			return 0;
		if (len < 1 || len > MAX_MESSAGE) {
			logmsg("request of %u bytes: not the agent protocol", len);
			return -1;
		}
		if (cur.n < len)
			return 0;
		cur.n = len;
		handle(cur.p[0], (struct cursor){ cur.p + 1, len - 1 }, &reply);
		buf_string(&framed, reply.data, reply.len);
		rc = write_all(c->fd, framed.data, framed.len);
		free(reply.data);
		free(framed.data);
		if (rc < 0)
			return -1;
		memmove(c->in.data, cur.p + len, c->in.len - 4 - len);
		c->in.len -= 4 + len;
	}
}

/* The listening socket: systemd's (LISTEN_FDS=1, fd 3), or else one at path,
 * for the owner alone. */
static int listen_socket(const char *path)
{
	struct sockaddr_un addr = { .sun_family = AF_UNIX };
	const char *pid = getenv("LISTEN_PID"), *fds = getenv("LISTEN_FDS");
	int fd;

	if (pid && fds && atol(pid) == getpid()) {
		if (strcmp(fds, "1"))
			fatal("expected one socket from systemd, got %s", fds);
		return 3;
	}
	if (!path)
		fatal("no socket: run from exocarp.socket, or give -l PATH");
	if (strlen(path) >= sizeof addr.sun_path)
		fatal("socket path too long: %s", path);
	strcpy(addr.sun_path, path);
	fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (fd < 0)
		fatal("socket: %s", strerror(errno));
	unlink(path);
	umask(077);
	if (bind(fd, (struct sockaddr *)&addr, sizeof addr) < 0 || chmod(path, 0600) < 0 ||
	    listen(fd, SOMAXCONN) < 0)
		fatal("cannot listen on %s: %s", path, strerror(errno));
	return fd;
}

static void usage(void)
{
	fatal("usage: exocarp [-T tcti] -p FILE\n       exocarp [-T tcti] [-l PATH]");
}

static void on_signal(int sig)
{
	(void)sig;
	stopping = 1;
}

int main(int argc, char **argv)
{
	const char *public_path = NULL, *listen_path = NULL;
	struct client clients[MAX_CLIENTS];
	struct pollfd fds[1 + MAX_CLIENTS];
	size_t nclients = 0, i;
	int opt, lfd;
	TSS2_RC rc;

	while ((opt = getopt(argc, argv, "T:p:l:")) != -1) {
		switch (opt) {
		case 'T':
			tcti_conf = optarg;
			break;
		case 'p':
			public_path = optarg;
			break;
		case 'l':
			listen_path = optarg;
			break;
		default:
			usage();
		}
	}
	if (optind != argc || (public_path && listen_path))
		usage();

	tpm_open();
	if (public_path) {
		write_public_key(public_path);
		tpm_close();
		return 0;
	}

	signal(SIGPIPE, SIG_IGN);
	/* poll() returns EINTR for these, whatever the handler's flags. */
	signal(SIGTERM, on_signal);
	signal(SIGINT, on_signal);
	lfd = listen_socket(listen_path);
	rc = key_create();
	if (rc)
		fatal("the TPM cannot create the host key: %s", Tss2_RC_Decode(rc));
	for (;;) {
		fds[0].fd = lfd;
		fds[0].events = nclients < MAX_CLIENTS ? POLLIN : 0;
		for (i = 0; i < nclients; i++) {
			fds[1 + i].fd = clients[i].fd;
			fds[1 + i].events = POLLIN;
		}
		if (poll(fds, 1 + nclients, -1) < 0) {
			if (errno != EINTR)
				fatal("poll: %s", strerror(errno));
			if (stopping)
				break;
			continue;
		}
		if (fds[0].revents & POLLIN) {
			int fd = accept4(lfd, NULL, NULL, SOCK_CLOEXEC);

			if (fd >= 0)
				clients[nclients++] = (struct client){ fd, { 0 } };
			else if (errno != EINTR && errno != EAGAIN && errno != ECONNABORTED)
				logmsg("accept: %s", strerror(errno));
		}
		for (i = 0; i < nclients;) {
			if (fds[1 + i].revents && serve(&clients[i]) < 0) {
				close(clients[i].fd);
				free(clients[i].in.data);
				clients[i] = clients[--nclients];
				/* The moved client's revents: fds[1 + nclients] holds them. */
				fds[1 + i] = fds[1 + nclients];
			} else {
				i++;
			}
		}
	}
	tpm_close();
	return 0;
}
