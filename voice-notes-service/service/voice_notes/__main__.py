"""CLI:  python -m voice_notes process lecture.m4a --out out/lecture
        python -m voice_notes selftest
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voice_notes")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("process", help="turn one recording into a v2.2 note")
    p.add_argument("audio")
    p.add_argument("--out", required=True, help="output folder for this job")
    p.add_argument("--title", default="")
    p.add_argument("--course", default="")
    p.add_argument("--professor", default="")
    p.add_argument("--pdf", action="store_true", help="also render a PDF (needs Chromium + Playwright)")
    sub.add_parser("selftest", help="offline checks (no AI calls, no cost)")
    args = parser.parse_args(argv)

    if args.cmd == "selftest":
        from .selftest import run

        return run()
    from .runner import process_audio

    result = process_audio(args.audio, out_dir=args.out, title=args.title, course_name=args.course,
                           professor_name=args.professor, make_pdf=args.pdf)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status == "COMPLETED" else 1


if __name__ == "__main__":
    sys.exit(main())
