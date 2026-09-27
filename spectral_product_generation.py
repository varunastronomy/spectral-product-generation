from pathlib import Path
import concurrent.futures
import os
import readline
import shutil
import shlex
import subprocess
import sys


standard_folders = []
obsid_names = []


def get_all_observations():
    return sorted(
        folder for folder in Path(".").iterdir()
        if folder.is_dir() and folder.name.isdigit()
    )


def get_observations_from_list(list_file):
    if not Path(list_file).is_file():
        print(f"List file not found: {list_file}")
        return []

    observations = []

    for line in Path(list_file).read_text().splitlines():
        name = line.strip()

        if not name:
            continue

        folder = Path(name)

        if folder.is_dir():
            observations.append(folder)
        else:
            print(f"Skipping {name}: observation folder not found")

    return observations


def complete_list_file(text, state):
    matches = [
        path.name
        for path in Path(".").iterdir()
        if path.is_file() and path.name.startswith(text) and path.suffix in [".txt", ".list"]
    ]

    if state < len(matches):
        return matches[state]

    return None


def get_standard_folders(observations):
    common_folders = None

    for obs in observations:
        folders = {folder.name for folder in obs.iterdir() if folder.is_dir()}

        if common_folders is None:
            common_folders = folders
        else:
            common_folders = common_folders & folders

    return sorted(common_folders or [])


def complete_folder(text, state):
    matches = [name for name in standard_folders if name.startswith(text)]

    if state < len(matches):
        return matches[state]

    return None


def complete_obsid(text, state):
    before_cursor = readline.get_line_buffer()

    if before_cursor.endswith(" "):
        current = ""
    else:
        current = before_cursor.split()[-1] if before_cursor.split() else text

    candidates = ["all"] + obsid_names
    matches = [name for name in candidates if name.startswith(current)]

    if state < len(matches):
        prefix = before_cursor[: len(before_cursor) - len(current)]
        return prefix + matches[state]

    return None


def expand_obsid_selection(selection, available_obsids):
    if selection == "all":
        return available_obsids

    selected = []

    for token in selection.split():
        if "-" in token:
            start, stop = token.split("-", 1)

            if start not in available_obsids or stop not in available_obsids:
                print(f"Selected ObsID range not found: {token}")
                raise SystemExit

            start_index = available_obsids.index(start)
            stop_index = available_obsids.index(stop)

            if start_index <= stop_index:
                range_obsids = available_obsids[start_index:stop_index + 1]
            else:
                range_obsids = available_obsids[stop_index:start_index + 1]

            for obsid in range_obsids:
                if obsid not in selected:
                    selected.append(obsid)
        else:
            if token not in available_obsids:
                print(f"Selected ObsID not found: {token}")
                raise SystemExit

            if token not in selected:
                selected.append(token)

    return selected


def infer_obsid_from_path(path, available_obsids):
    for part in path.parts:
        if part in available_obsids:
            return part

    return None


def load_gti_files(list_file, available_obsids, folder_name):
    if not list_file:
        return {}

    list_path = Path(list_file).expanduser()

    if not list_path.is_file():
        print(f"GTI list file not found: {list_file}")
        raise SystemExit

    gti_files = {}
    base_dir = list_path.parent

    for line_number, raw_line in enumerate(list_path.read_text().splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()

        if not line:
            continue

        tokens = line.replace(",", " ").split()

        if len(tokens) >= 2 and tokens[0] in available_obsids:
            obsid = tokens[0]
            gti_text = tokens[1]
        else:
            gti_text = tokens[0]
            gti_path_for_obsid = Path(gti_text).expanduser()
            obsid = infer_obsid_from_path(gti_path_for_obsid, available_obsids)

        if not obsid:
            print(f"Could not infer ObsID from GTI list line {line_number}: {raw_line}")
            raise SystemExit

        gti_path = Path(gti_text).expanduser()

        if not gti_path.is_absolute():
            candidates = [
                Path.cwd() / gti_path,
                base_dir / gti_path,
                Path.cwd() / obsid / folder_name / gti_path,
                Path.cwd() / obsid / gti_path,
            ]
            gti_path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])

        if not gti_path.is_file():
            print(f"GTI file not found for ObsID {obsid}: {gti_path}")
            raise SystemExit

        gti_files[obsid] = gti_path.resolve()

    return gti_files


def process_observation(obs, folder_name, gti_files):
    work_dir = obs / folder_name
    output_lines = [
        "###",
        f"ObsID: {obs.name}",
        f"Working directory: {work_dir}",
    ]

    try:
        spectra_dir = work_dir / "spectra"
        spectra_dir.mkdir(exist_ok=True)

        existing_files = {
            path.resolve()
            for path in work_dir.iterdir()
            if path.is_file()
        }

        gti_file = gti_files.get(obs.name)
        command = [
            "nicerl3-spect",
            "indir=./",
            "grouptype=optmin",
            "bkgformat=file",
            "bkgmodeltype=scorpeon",
            "clobber=yes",
            "syserrfile=NONE",
        ]

        if gti_file:
            command.append(f"gtifile={gti_file}")
            output_lines.append(f"GTI file: {gti_file}")
        elif gti_files:
            output_lines.append("GTI file: not listed for this ObsID; using default nicerl3-spect command")

        output_lines.append("Command: " + " ".join(shlex.quote(str(part)) for part in command))
        result = subprocess.run(command, cwd=work_dir, capture_output=True, text=True)

        if result.stdout:
            output_lines.append(result.stdout.rstrip())

        if result.stderr:
            output_lines.append(result.stderr.rstrip())

        if result.returncode != 0:
            output_lines.append(f"Skipping file move for {work_dir}: command failed")
            output_lines.append("###")
            return {
                "success": False,
                "obsid": obs.name,
                "work_dir": str(work_dir.resolve()),
                "spectra_dir": "",
                "moved_count": 0,
                "reason": f"nicerl3-spect_returncode_{result.returncode}",
                "output": "\n".join(output_lines),
            }

        generated_files = sorted(
            path
            for path in work_dir.iterdir()
            if path.is_file() and path.resolve() not in existing_files
        )

        if not generated_files:
            output_lines.append(f"No new spectra files found in {work_dir}")
            output_lines.append("###")
            return {
                "success": False,
                "obsid": obs.name,
                "work_dir": str(work_dir.resolve()),
                "spectra_dir": "",
                "moved_count": 0,
                "reason": "no_new_spectra_files",
                "output": "\n".join(output_lines),
            }

        for path in generated_files:
            shutil.move(str(path), spectra_dir / path.name)

        output_lines.append(f"Moved {len(generated_files)} generated file(s) to {spectra_dir}")
        output_lines.append("###")
        return {
            "success": True,
            "obsid": obs.name,
            "work_dir": str(work_dir.resolve()),
            "spectra_dir": str(spectra_dir.resolve()),
            "moved_count": len(generated_files),
            "reason": "",
            "output": "\n".join(output_lines),
        }
    except Exception as exc:
        output_lines.append(f"Failed in {work_dir}: {exc}")
        output_lines.append("###")
        return {
            "success": False,
            "obsid": obs.name,
            "work_dir": str(work_dir.resolve()),
            "spectra_dir": "",
            "moved_count": 0,
            "reason": f"exception_{type(exc).__name__}:{exc}",
            "output": "\n".join(output_lines),
        }


if len(sys.argv) > 1:
    list_file = sys.argv[1]
    observations = get_observations_from_list(list_file)
else:
    readline.parse_and_bind("tab: complete")
    readline.set_completer(complete_list_file)

    list_file = input("ObsID list file [obs_list.txt]: ").strip() or "obs_list.txt"
    observations = get_observations_from_list(list_file)

if not observations:
    print("No observation folders found")
    raise SystemExit

obsid_names = [obs.name for obs in observations]

print("\nObsIDs:")
print("all")
for name in obsid_names:
    print(name)

readline.parse_and_bind("tab: complete")
readline.set_completer(complete_obsid)
selected_obsid_input = input("\nObsID(s), spaces or ranges [all]: ").strip() or "all"

if selected_obsid_input == "all":
    selected_observations = observations
else:
    selected_names = expand_obsid_selection(selected_obsid_input, obsid_names)

    selected_observations = [
        obs for obs in observations
        if obs.name in selected_names
    ]

standard_folders = get_standard_folders(selected_observations)

if not standard_folders:
    print("No standard folders found")
    raise SystemExit

readline.parse_and_bind("tab: complete")
readline.set_completer(complete_folder)

print("Standard folders:")
for folder in standard_folders:
    print(folder)

default_folder = "products" if "products" in standard_folders else standard_folders[0]
folder_name = input(f"\nEnter folder name [{default_folder}]: ").strip() or default_folder

if folder_name not in standard_folders:
    print("Invalid folder name")
    raise SystemExit

readline.parse_and_bind("tab: complete")
readline.set_completer(complete_list_file)
gti_list_file = input("GTI list file [none]: ").strip()
gti_files = load_gti_files(gti_list_file, obsid_names, folder_name) if gti_list_file else {}

if gti_files:
    print(f"Loaded {len(gti_files)} GTI file(s) from {gti_list_file}")
else:
    print("No GTI list supplied; using default nicerl3-spect command without gtifile")

write_logs = input("Write log files? y/n [n]: ").strip().lower() in {"y", "yes"}

success_log = []
failure_log = []
max_workers = os.cpu_count() or 1

print(f"\nRunning {len(selected_observations)} ObsID(s) with {max_workers} CPU core(s)")

with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
    future_to_obs = {
        executor.submit(process_observation, obs, folder_name, gti_files): obs
        for obs in selected_observations
    }

    for future in concurrent.futures.as_completed(future_to_obs):
        result = future.result()
        print(result["output"])

        if result["success"]:
            success_log.append(
                (
                    result["obsid"],
                    result["work_dir"],
                    result["spectra_dir"],
                    result["moved_count"],
                )
            )
        else:
            failure_log.append((result["obsid"], result["work_dir"], result["reason"]))

if write_logs:
    success_log_file = "nicer_spectra_pipeline_output_folders.log"
    failure_log_file = "nicer_spectra_pipeline_failed_obs.log"

    with open(success_log_file, "w") as f:
        for obsid, work_dir, spectra_dir, moved_count in success_log:
            f.write(f"{obsid}\n")

    with open(failure_log_file, "w") as f:
        for obsid, work_dir, reason in failure_log:
            f.write(f"{obsid}\n")

    print("\nSaved output folder log:", success_log_file)
    print("Saved failed ObsID log:", failure_log_file)
else:
    print("\nLog files were not written")
