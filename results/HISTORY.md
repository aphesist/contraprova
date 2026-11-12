# History note: commit hashes

On 2026-11-12 one commit's committer field was corrected (it named the wrong committer; its
author, date and content were already right). Rewriting that field changed the hash of that commit and
of every commit after it. No file content, author, or author or committer date changed: each
rewritten commit has the same tree as the one it replaces.

Hashes cited in `results/` were updated to the new ones, except inside pre-registration files, which
stay byte-for-byte as committed. Old to new:

| Old | New | What it is |
|---|---|---|
| 796279e | f39dc2b | The corrected commit (lab kit references) |
| 0414b74 | c32ed65 | Probe 2 PREREG |
| 0d80260 | b133048 | Harness changes before probe 3 (cited in `swepro-probe3/PREREG.md`) |
| 851c93d | 14d92e2 | Probe 3 PREREG |
| 8d92b0a | f43ad38 | Main study PREREG |
| 2d12cc5 | b587f67 | Validation pulls through the image cache |

The commit dates in `git log` still show each PREREG was committed before its run.
