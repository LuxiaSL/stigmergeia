/* shmredir.c — POSIX shared memory and named semaphores, relocated.
 *
 * glibc hard-codes /dev/shm for shm_open() and sem_open(). On a shared
 * node, /dev/shm holds other jobs' live state (the shared-memory transport
 * of running GPU jobs, KV caches, dataset caches), so the jail
 * cannot grant it: a Landlock rule on /dev/shm covers every segment in it.
 * Without these calls, Python's multiprocessing (SemLock) and torch
 * DataLoader workers fail with EACCES.
 *
 * Preloaded into jailed processes, this library serves the same calls from
 * $SWARM_SHM_DIR (the jail's own <workspace>/.shm) instead. Semantics kept:
 * O_CREAT/O_EXCL, unlink-while-mapped, and a named semaphore is initialised
 * BEFORE its name becomes visible (create under a temp name, then link()).
 *
 * Build: gcc -O2 -Wall -shared -fPIC -o libshmredir.so shmredir.c -pthread
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <semaphore.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

/* "/name" -> "$SWARM_SHM_DIR/name"; exactly one leading slash allowed,
 * none inside (POSIX portable names), no "." or "..". */
static int shm_path(const char *name, char *out, size_t cap) {
    const char *dir = getenv("SWARM_SHM_DIR");
    if (!dir || !*dir) { errno = EACCES; return -1; }
    while (*name == '/') name++;
    if (!*name || strchr(name, '/') || !strcmp(name, ".") || !strcmp(name, "..")) {
        errno = EINVAL; return -1;
    }
    int n = snprintf(out, cap, "%s/%s", dir, name);
    if (n < 0 || (size_t)n >= cap) { errno = ENAMETOOLONG; return -1; }
    return 0;
}

int shm_open(const char *name, int oflag, mode_t mode) {
    char path[PATH_MAX];
    if (shm_path(name, path, sizeof path) < 0) return -1;
    return open(path, oflag | O_NOFOLLOW | O_CLOEXEC, mode);
}

int shm_unlink(const char *name) {
    char path[PATH_MAX];
    if (shm_path(name, path, sizeof path) < 0) return -1;
    return unlink(path);
}

static sem_t *map_sem(int fd) {
    void *p = mmap(NULL, sizeof(sem_t), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    return p == MAP_FAILED ? SEM_FAILED : (sem_t *)p;
}

static sem_t *open_existing(const char *path) {
    int fd = open(path, O_RDWR | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) return SEM_FAILED;
    sem_t *s = map_sem(fd);
    int saved = errno;
    close(fd);
    errno = saved;
    return s;
}

sem_t *sem_open(const char *name, int oflag, ...) {
    char path[PATH_MAX];
    if (shm_path(name, path, sizeof path) < 0) return SEM_FAILED;
    if (!(oflag & O_CREAT)) return open_existing(path);

    va_list ap;
    va_start(ap, oflag);
    mode_t mode = (mode_t)va_arg(ap, unsigned int);
    unsigned int value = va_arg(ap, unsigned int);
    va_end(ap);
    if (value > SEM_VALUE_MAX) { errno = EINVAL; return SEM_FAILED; }

    for (;;) {
        /* Initialise under a private temp name, then publish atomically. */
        char tmp[PATH_MAX];
        int n = snprintf(tmp, sizeof tmp, "%s.XXXXXX", path);
        if (n < 0 || (size_t)n >= sizeof tmp) { errno = ENAMETOOLONG; return SEM_FAILED; }
        int fd = mkostemp(tmp, O_CLOEXEC);
        if (fd < 0) return SEM_FAILED;
        fchmod(fd, mode & 0777);
        sem_t *s = SEM_FAILED;
        if (ftruncate(fd, sizeof(sem_t)) == 0 && (s = map_sem(fd)) != SEM_FAILED
            && sem_init(s, 1, value) == 0) {
            if (link(tmp, path) == 0) {
                unlink(tmp);
                close(fd);
                return s;
            }
        }
        int saved = errno;
        if (s != SEM_FAILED) munmap(s, sizeof(sem_t));
        unlink(tmp);
        close(fd);
        if (saved != EEXIST) { errno = saved; return SEM_FAILED; }
        if (oflag & O_EXCL) { errno = EEXIST; return SEM_FAILED; }
        sem_t *existing = open_existing(path);
        if (existing != SEM_FAILED || errno != ENOENT) return existing;
        /* raced with an unlink: try creating again */
    }
}

int sem_close(sem_t *sem) {
    return munmap(sem, sizeof(sem_t));
}

int sem_unlink(const char *name) {
    char path[PATH_MAX];
    if (shm_path(name, path, sizeof path) < 0) return -1;
    return unlink(path);
}
