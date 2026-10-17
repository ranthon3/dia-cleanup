/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Native bundle entry point; the resource script configures GTK and Python. */
#include <mach-o/dyld.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int
main (int argc, char **argv)
{
  uint32_t size = 0;
  char *executable;
  char *resolved;
  char *slash;
  char *script;
  char **arguments;

  _NSGetExecutablePath (NULL, &size);
  executable = malloc (size);
  if (!executable || _NSGetExecutablePath (executable, &size) != 0) {
    fputs ("Cannot locate the Dia application\n", stderr);
    return 1;
  }
  resolved = realpath (executable, NULL);
  free (executable);
  if (!resolved || !(slash = strrchr (resolved, '/'))) {
    fputs ("Cannot resolve the Dia application path\n", stderr);
    free (resolved);
    return 1;
  }
  *slash = '\0';
  if (asprintf (&script, "%s/../Resources/launch.sh", resolved) < 0) {
    free (resolved);
    return 1;
  }
  free (resolved);
  arguments = calloc ((size_t) argc + 2, sizeof (*arguments));
  if (!arguments) {
    free (script);
    return 1;
  }
  arguments[0] = "/bin/sh";
  arguments[1] = script;
  for (int i = 1; i < argc; i++) {
    arguments[i + 1] = argv[i];
  }
  execv (arguments[0], arguments);
  perror ("Cannot launch Dia");
  free (arguments);
  free (script);
  return 1;
}
