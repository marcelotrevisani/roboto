import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import requests
from fastapi import FastAPI
from fastapi_utils.tasks import repeat_every
from grayskull.__main__ import create_python_recipe
from grayskull.cli import CLIConfig
from grayskull.strategy.py_base import download_sdist_pkg
from souschef.recipe import Recipe

app = FastAPI()
GH_TOKEN = os.getenv("GH_TOKEN")
CHECK_NOTIFICATIONS_INTERVAL = 60 * 4


def send_comment(issue_url: str, msg: str):
    response = requests.post(
        f"{issue_url}/comments",
        headers={"Authorization": f"token {GH_TOKEN}"},
        data=json.dumps({"body": msg}),
    )
    response.raise_for_status()


def get_issue_info(issue_url):
    issue_info = requests.get(issue_url)
    issue_info.raise_for_status()
    return issue_info.json()


def get_pr_info(pr_url):
    pr_info = requests.get(pr_url)
    pr_info.raise_for_status()
    return pr_info.json()


def convert_to_str(ingredient):
    str_dep = f"{ingredient.package_name}"
    if ingredient.constrains:
        str_dep += f" {ingredient.constrains}"
    if ingredient.selector:
        str_dep += f"  # [{ingredient.selector}]"
    return str_dep


def get_table_deps(current_recipe, gs_recipe, req_section):
    table = f"================ **{req_section.upper()}** ================"
    table += f"\nRequirements for **{req_section}**\n"
    table += "| Current Deps | Grayskull found |  |\n"
    table += "|--------------|-----------------|--|\n"
    for current_dep in current_recipe["requirements"][req_section]:
        for gs_dep in gs_recipe["requirements"][req_section] or []:
            if gs_dep.package_name == current_dep.package_name:
                if (
                    gs_dep.constrains == current_dep.constrains
                    and gs_dep.selector == current_dep.selector
                ):
                    str_dep = convert_to_str(gs_dep)
                    table += f"| {str_dep} | {str_dep} | :heavy_check_mark: |\n"
                else:
                    table += (
                        f"| {convert_to_str(current_dep)} |"
                        f" {convert_to_str(gs_dep)} | :heavy_exclamation_mark: |\n"
                    )
                break

        else:
            str_dep = convert_to_str(current_dep)
            table += f"| {str_dep} |  | :x: |\n"

    for gs_dep in gs_recipe["requirements"][req_section] or []:
        if gs_dep.package_name not in (
            current_recipe["requirements"][req_section] or []
        ):
            table += f"| | {convert_to_str(gs_dep)} | :heavy_plus_sign: |\n"
    return table


def get_gs_message_show_requirements(recipe: Recipe, gs_recipe: Recipe) -> str:
    msg = ""
    if "build" in recipe["requirements"]:
        msg += get_table_deps(recipe, gs_recipe, "build")
        msg += "\n\n"
    if "host" in recipe["requirements"]:
        msg += get_table_deps(recipe, gs_recipe, "host")
        msg += "\n\n"
    if "run" in recipe["requirements"]:
        msg += get_table_deps(recipe, gs_recipe, "run")
    return msg


def _extract_send_requirements(pr_json, folder, render_cb, response_msg):
    subprocess.run(
        [
            "git",
            "clone",
            pr_json["head"]["repo"]["git_url"],
            folder,
            "--branch",
            pr_json["head"]["ref"],
        ],
        check=True,
    )
    recipe_path = Path(folder) / "recipe" / "meta.yaml"
    if not recipe_path.is_file():
        recipe_path = Path(folder) / "recipe" / "meta.yml"
        if not recipe_path.is_file():
            raise ValueError(
                "There is no recipe file in recipe folder (meta.yaml or meta.yml)"
                f" - {pr_json['head']['repo']['git_url']}"
            )
    recipe = Recipe(load_file=recipe_path, show_comments=False)

    rendered_recipe = render_cb([str(Path(folder) / "recipe")], print_results=False)
    pkg_url = rendered_recipe[0][0].meta["source"]["url"]
    pkg_file_name = pkg_url.strip().split("/")[-1]

    with tempfile.TemporaryDirectory() as sdist_folder:
        CLIConfig(stdout=False)
        download_sdist_pkg(pkg_url, Path(sdist_folder) / pkg_file_name)
        sdist_file = Path(sdist_folder) / pkg_file_name
        gs_recipe = create_python_recipe(
            str(sdist_file),
            is_strict_cf=True,
            from_local_sdist=True,
        )[0]
        send_comment(
            response_msg["issue_url"],
            get_gs_message_show_requirements(recipe, gs_recipe),
        )


def show_requirements(response_msg: dict):
    from conda_build.cli.main_render import execute as render_cb

    issue_json = get_issue_info(response_msg["issue_url"])
    pr_json = get_pr_info(issue_json["pull_request"]["url"])

    with tempfile.TemporaryDirectory() as folder:
        _extract_send_requirements(pr_json, folder, render_cb, response_msg)


def run_command_msg(response_msg):
    msg = response_msg["body"]
    all_cmds = {
        show_requirements: re.compile(r"@conda\-grayskull\s+show\s+requirement[s]*")
    }
    for run_function, re_match in all_cmds.items():
        if re_match.match(msg):
            run_function(response_msg)
            break
    else:
        send_comment(
            response_msg["issue_url"],
            "Command not recognized, please inform a valid command.",
        )


@app.on_event("startup")
@repeat_every(seconds=CHECK_NOTIFICATIONS_INTERVAL)
def check_notifications():
    response = requests.get(
        "https://api.github.com/notifications",
        params={"reason": "mention", "unread": True},
        headers={"Authorization": f"token {GH_TOKEN}"},
    )
    response.raise_for_status()
    all_mentions = response.json()
    last_update = None
    for mention in all_mentions:
        mention_updated_at = mention["updated_at"]
        if mention_updated_at.endswith("Z"):
            mention_updated_at = mention_updated_at[:-1]
        mention_updated_at = datetime.fromisoformat(mention_updated_at)
        if last_update:
            last_update = max(mention_updated_at, last_update)
        else:
            last_update = mention_updated_at

        response = requests.get(mention["subject"]["latest_comment_url"])
        response.raise_for_status()
        msg = response.json()

        send_comment(msg["issue_url"], "Working on your request...")

        run_command_msg(msg)
    if all_mentions:
        requests.put(
            "https://api.github.com/notifications",
            headers={"Authorization": f"token {GH_TOKEN}"},
            params={"last_read_at": last_update, "read": True},
        )
