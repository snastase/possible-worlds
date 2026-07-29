import sys
import time
import json
import select
import termios
import tty
import argparse
from pathlib import Path
from datetime import datetime, timezone
import os
import pty
import subprocess
import re


ZORK_PROMPT_RE = re.compile(r"(?:^|\n)>\s*$")


def wait_for_trigger():
    stdin_fd = sys.stdin.fileno()
    original_settings = termios.tcgetattr(stdin_fd)

    try:
        tty.setcbreak(stdin_fd)
        print("Waiting for scanner...")

        while True:
            ready, _, _ = select.select([stdin_fd], [], [], 0.05)
            if ready:
                ch = sys.stdin.read(1)
                t = time.monotonic()

                print(f"received: {ch!r} at {t:.6f}")

                if ch == "=":
                    print("Scanner trigger received!")
                    return ch, t
                elif ch in {"q", "\x1b"}:
                    print("Quitting out!")
                    return ch, t
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, original_settings)


def launch_game(game_path):
    master_fd, slave_fd = pty.openpty()

    attrs = termios.tcgetattr(slave_fd)
    attrs[3] &= ~termios.ECHO
    termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)

    proc = subprocess.Popen(
        ["dfrotz", str(game_path)],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )

    os.close(slave_fd)
    return proc, master_fd


def normalize_turn_text(turn_text, prompt_re=ZORK_PROMPT_RE):
    raw_text = turn_text
    response_text = prompt_re.sub("", turn_text)
    response_text = response_text.rstrip() + "\n"
    return raw_text, response_text


def read_turn(
    master_fd,
    prompt_re=ZORK_PROMPT_RE,
    quiet_time=0.25,
    overall_timeout=10.0,
):
    turn_start = time.monotonic()
    last_output_time = turn_start
    raw_chunks = []
    turn_text = ""
    saw_eof = False

    while True:
        ready, _, _ = select.select([master_fd], [], [], 0.05)

        if ready:
            chunk = os.read(master_fd, 4096)
            if not chunk:
                saw_eof = True
                break

            now = time.monotonic()
            text = chunk.decode("utf-8", errors="replace")

            raw_chunks.append({
                "t_monotonic": now,
                "text": text,
            })
            turn_text += text
            last_output_time = now

            if prompt_re.search(turn_text):
                raw_text, response_text = normalize_turn_text(turn_text, prompt_re)
                return {
                    "raw_text": raw_text,
                    "response_text": response_text,
                    "raw_chunks": raw_chunks,
                    "got_prompt": True,
                    "turn_start": turn_start,
                    "turn_end": last_output_time,
                    "eof": False,
                }

        now = time.monotonic()

        if now - turn_start > overall_timeout:
            break

        if turn_text and (now - last_output_time > quiet_time):
            break

    raw_text, response_text = normalize_turn_text(turn_text, prompt_re)
    return {
        "raw_text": raw_text,
        "response_text": response_text,
        "raw_chunks": raw_chunks,
        "got_prompt": bool(prompt_re.search(turn_text)),
        "turn_start": turn_start,
        "turn_end": last_output_time,
        "eof": saw_eof,
    }


def send_command(master_fd, command):
    os.write(master_fd, (command + "\r").encode("utf-8"))


def write_event(events_f, event_type, **payload):
    record = {
        "event": event_type,
        "t_monotonic": time.monotonic(),
        "t_utc": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    with events_f.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-sub", "--subject", required=True, type=int, help="e.g. 01")
    parser.add_argument("-ses", "--session", required=True, type=int, help="e.g. 001")
    parser.add_argument("-run", "--run", required=True, type=int, help="e.g. 01")
    parser.add_argument("-task", "--task", required=True, help="e.g. zork1")
    parser.add_argument("-g", "--game", required=True, type=Path, help="path to zork1.z3")
    parser.add_argument("--data-root", default=Path("data"), type=Path)
    args = parser.parse_args()

    if not args.game.exists():
        raise FileNotFoundError(f"Game file not found: {args.game}")

    subject = f"sub-{args.subject:02d}"
    session = f"ses-{args.session:03d}"
    run = f"run-{args.run:02d}"
    task = f"task-{args.task}"

    subject_dir = args.data_root / subject
    session_dir = subject_dir / session

    subject_dir.mkdir(parents=True, exist_ok=True)
    session_dir.mkdir(parents=True, exist_ok=True)

    session_metadata_f = session_dir / "metadata.json"
    if not session_metadata_f.exists():
        session_metadata = {
            "subject": subject,
            "session": session,
            "created_utc": datetime.now(timezone.utc).isoformat(),
        }
        session_metadata_f.write_text(
            json.dumps(session_metadata, indent=2),
            encoding="utf-8",
        )

    events_f = session_dir / f"{subject}_{session}_{task}_{run}_events.jsonl"

    print(f"Starting subject/session: {subject}/{session}")
    print(f"Session directory: {session_dir}")
    print(f"Events file: {events_f.name}")
    print(f"Game file: {args.game.resolve()}")
    
    trigger, trigger_time = wait_for_trigger()
    if trigger == "q":
        print("Aborted before game launch")
        return

    write_event(
        events_f,
        "scanner_trigger",
        key=trigger,
        trigger_time_monotonic=trigger_time,
        onset=0.0,
        subject=subject,
        session=session,
        run=run,
        task=task,
        game_path=str(args.game.resolve()),
        events_file=events_f.name,
    )

    proc, master_fd = launch_game(args.game)

    write_event(
        events_f,
        "game_launch",
        game_path=str(args.game.resolve()),
        subject=subject,
        session=session,
        run=run,
        task=task,
    )

    startup_turn = read_turn(master_fd)
    print(startup_turn["response_text"])

    write_event(
        events_f,
        "game_turn",
        turn_index=0,
        command=None,
        response_text=startup_turn["response_text"],
        turn_start=startup_turn["turn_start"],
        turn_end=startup_turn["turn_end"],
        onset=startup_turn["turn_start"] - trigger_time,
        offset=startup_turn["turn_end"] - trigger_time,
        got_prompt=startup_turn["got_prompt"],
        raw_chunks=startup_turn["raw_chunks"],   # probably temporary
     )

    pending_quit = False
    turn_index = 1

    try:
        while True:
            command = input("> ").strip()

            if not command:
                continue
            command_time = time.monotonic()

            write_event(
                events_f,
                "player_input",
                turn_index=turn_index,
                command=command,
                command_time_monotonic=command_time,
                onset=command_time - trigger_time,
                subject=subject,
                session=session,
                run=run,
                task=task,
            )

            send_command(master_fd, command)
            turn = read_turn(master_fd)
            if turn is None:
                break
            if turn["response_text"]:
                print(turn["response_text"])

            write_event(
                events_f,
                "game_turn",
                turn_index=turn_index,
                command=command,
                response_text=turn["response_text"],   # cleaned
                raw_text=turn["raw_text"],             # exact PTY output
                turn_start=turn["turn_start"],
                turn_end=turn["turn_end"],
                onset=turn["turn_start"] - trigger_time,
                offset=turn["turn_end"] - trigger_time,
                got_prompt=turn["got_prompt"],
                eof=turn["eof"],
                raw_chunks=turn["raw_chunks"],         # temporary/debug
                subject=subject,
                session=session,
                run=run,
                task=task,
            )

            if pending_quit and command.lower() == "y":
                write_event(
                    events_f,
                    "run_end",
                    status="completed",
                    subject=subject,
                    session=session,
                    run=run,
                    task=task,
                )
                break

            pending_quit = command.lower() == "quit"

            turn_index += 1

    except KeyboardInterrupt:
        write_event(
            events_f,
            "run_end",
            status="interrupt",
            subject=subject,
            session=session,
            run=run,
            task=task,
        )
        print("\nKeyboard-interrupted!")


if __name__ == "__main__":
    main()
