#!/usr/bin/env python3
"""
Find files that contain at least one sensitive word, and print every match
found in each file.

Usage:
    python3 find_sensitive.py WORDS_FILE PATH [PATH ...] [options]

Each PATH can be a file, a directory (searched recursively) or a wildcard
pattern such as 'logs/*.txt' or 'src/**/*.py'. Paths are processed one by
one, in the order given. Patterns are expanded by the script itself when
quoted, so they also work on Windows and support ** for any depth.

The words file holds one word or phrase per line. Blank lines and lines
starting with '#' are ignored.

Defaults: matching is case-insensitive, and binary files are scanned too
(their bytes are decoded as UTF-8, so readable text inside them is found).

Exit status (grep-style):
    0  at least one file matched
    1  no files matched
    2  a problem occurred (missing path, pattern with no matches,
       unreadable file), even if other files matched
"""

import argparse
import glob
import os
import re
import sys

# Lines are read in pieces of at most this many characters, so a binary file
# with no newlines never has to fit in memory all at once.
PIECE_SIZE = 1 << 20
CONTEXT = 40  # characters of surrounding text shown on each side of a match
GLOB_CHARS = re.compile(r"[*?\[]")

problem_count = 0


def warn(message):
    global problem_count
    problem_count += 1
    print(f"warning: {message}", file=sys.stderr)


def load_words(path):
    words = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            word = line.strip()
            if word and not word.startswith("#"):
                words.append(word)
    return list(dict.fromkeys(words))  # de-duplicate, keep order


def build_pattern(words, case_sensitive, whole_word):
    # Longest first, so "password" wins over "pass" when both could match.
    alternatives = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    if whole_word:
        # Lookarounds instead of \b so words that start or end with
        # punctuation (e.g. "@internal", "c++") still match correctly.
        pattern = rf"(?<!\w)(?:{alternatives})(?!\w)"
    else:
        pattern = rf"(?:{alternatives})"
    return re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)


def is_binary(path, blocksize=8192):
    with open(path, "rb") as f:
        return b"\0" in f.read(blocksize)


def walk_directory(root, exclude_dirs):
    def on_error(err):
        warn(f"cannot read directory {err.filename}: {err.strerror}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        dirnames[:] = sorted(d for d in dirnames if d not in exclude_dirs)
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if os.path.isfile(path):  # skips sockets, broken symlinks, etc.
                yield path


def iter_targets(args, exclude_dirs):
    """Yield every file to scan, going through the arguments one by one."""
    for arg in args:
        arg = os.path.expanduser(arg)

        # A path that exists is taken literally, even if its name contains
        # wildcard characters; anything else with wildcards is a pattern.
        if os.path.lexists(arg) or not GLOB_CHARS.search(arg):
            paths, from_pattern = [arg], False
        else:
            paths, from_pattern = sorted(glob.glob(arg, recursive=True)), True
            if not paths:
                warn(f"no files match pattern: {arg}")
                continue

        for path in paths:
            if os.path.isdir(path):
                # Directories named explicitly are always searched; ones a
                # pattern expanded to respect --exclude-dir.
                name = os.path.basename(os.path.normpath(path))
                if from_pattern and name in exclude_dirs:
                    continue
                yield from walk_directory(path, exclude_dirs)
            elif os.path.isfile(path):
                yield path
            elif not os.path.exists(path):
                warn(f"no such file or directory: {path}")
            elif not from_pattern:
                warn(f"not a regular file, skipped: {path}")


def find_matches(path, pattern, overlap, stop_at_first):
    """Yield (line_number, text, match) for every match in the file."""
    with open(path, encoding="utf-8", errors="replace") as f:
        lineno, carry = 1, ""
        while piece := f.readline(PIECE_SIZE):
            text = carry + piece
            for match in pattern.finditer(text):
                if match.end() <= len(carry):
                    continue  # lies entirely in carried-over text: already reported
                yield lineno, text, match
                if stop_at_first:
                    return
            if piece.endswith("\n"):
                lineno, carry = lineno + 1, ""
            else:
                # A long line was split into pieces: keep its tail so a word
                # straddling the split is still found.
                carry = text[-overlap:] if overlap else ""


def clean(s):
    """Make text safe to print on one terminal line (binary junk becomes '.')."""
    return "".join(
        " " if c == "\t" else c if c.isprintable() and c != "�" else "."
        for c in s
    )


def context(text, match, highlight):
    """The matched text with up to CONTEXT characters on each side."""
    line = text.rstrip("\r\n")
    start, end = match.span()
    left, right = max(0, start - CONTEXT), min(len(line), end + CONTEXT)
    before = clean(line[left:start])
    if left == 0:
        before = before.lstrip()
    prefix = "..." if left > 0 else ""
    suffix = "..." if right < len(line) else ""
    return f"{prefix}{before}{highlight(clean(match.group(0)))}{clean(line[end:right])}{suffix}"


def main():
    parser = argparse.ArgumentParser(
        description="Find files that contain words from WORDS_FILE and print every match.",
        epilog="Quote wildcard patterns (e.g. 'logs/*.txt') to let the script expand them; "
               "this also enables ** to match any number of subdirectories.",
    )
    parser.add_argument("words_file", help="file with one sensitive word or phrase per line")
    parser.add_argument("paths", nargs="+", metavar="PATH",
                        help="file, directory (searched recursively) or wildcard pattern")
    parser.add_argument("-c", "--case-sensitive", action="store_true",
                        help="match case exactly (default: case-insensitive)")
    parser.add_argument("-w", "--whole-word", action="store_true",
                        help="only match whole words, so 'pass' won't match 'compass'")
    parser.add_argument("-l", "--files-only", action="store_true",
                        help="print only the matching file paths, not the matches")
    parser.add_argument("--skip-binary", action="store_true",
                        help="don't scan files that look binary (default: scan them)")
    parser.add_argument("--exclude-dir", action="append", default=[], metavar="NAME",
                        help="directory name to skip while searching, e.g. .git (repeatable)")
    args = parser.parse_args()

    if not os.path.isfile(args.words_file):
        parser.exit(2, f"error: words file not found: {args.words_file}\n")

    words = load_words(args.words_file)
    if not words:
        parser.exit(2, f"error: no words found in {args.words_file}\n")

    pattern = build_pattern(words, args.case_sensitive, args.whole_word)
    overlap = max(len(w) for w in words) - 1

    use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def paint(code, s):
        return f"\033[{code}m{s}\033[0m" if use_color else s

    def highlight(s):
        return paint("1;31", s)

    # Files already handled, so a file reached twice (e.g. named directly and
    # also inside a directory argument) is only scanned once. The word list
    # itself is pre-seeded because it would always match.
    seen = {os.path.realpath(args.words_file)}

    scanned = matched_files = total_matches = 0
    for path in iter_targets(args.paths, set(args.exclude_dir)):
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        try:
            binary = is_binary(path)
            if binary and args.skip_binary:
                continue
            scanned += 1
            # Build the printable form right away so large pieces of text
            # aren't kept in memory for every match.
            hits = [
                (lineno, clean(m.group(0)), context(text, m, highlight))
                for lineno, text, m in find_matches(path, pattern, overlap, args.files_only)
            ]
        except OSError as err:
            warn(f"cannot read {path}: {err.strerror}")
            continue

        if not hits:
            continue
        matched_files += 1
        total_matches += len(hits)

        if args.files_only:
            print(path, flush=True)
            continue

        count = f"{len(hits)} match{'' if len(hits) == 1 else 'es'}"
        print(f"{paint('1;35', path)}  ({count}{', binary' if binary else ''})")
        num_width = len(str(hits[-1][0]))
        word_width = max(len(found) for _, found, _ in hits)
        for lineno, found, ctx in hits:
            pad = " " * (word_width - len(found))
            print(f"  line {lineno:>{num_width}}  {highlight(found)}{pad}  {ctx}")
        print(flush=True)

    if not args.files_only:
        summary = (f"{matched_files} of {scanned} files scanned contain sensitive words "
                   f"({total_matches} match{'' if total_matches == 1 else 'es'})")
        if problem_count:
            summary += f"; {problem_count} warning{'' if problem_count == 1 else 's'}, see above"
        print(summary + ".", file=sys.stderr)

    if problem_count:
        return 2
    return 0 if matched_files else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
