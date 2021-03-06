"""
Main processing for meticulous
"""
from __future__ import absolute_import, division, print_function

import io
import re
import sys
from pathlib import Path

from github import GithubException
from plumbum import FG, ProcessExecutionError, local

from meticulous._exceptions import ProcessingFailed
from meticulous._github import create_pr, get_parent_repo
from meticulous._input import (
    UserCancel,
    get_confirmation,
    make_choice,
    make_simple_choice,
)
from meticulous._processrepo import add_repo_save
from meticulous._storage import get_json_value
from meticulous._summary import display_and_check_files
from meticulous._util import get_editor


def submit_handlers():
    """
    Obtain multithread task handlers for submission.
    """
    return {"submit": submit, "plain_pr": plain_pr, "full_pr": full_pr}


def submit(context):
    """
    Task to submit a pull request/issue
    repository is clean
    """

    def handler():
        reponame = context.taskjson["reponame"]
        repository_saves = get_json_value("repository_saves", {})
        if reponame in repository_saves:
            reposave = repository_saves[reponame]
            suggest_plain = check_if_plain_pr(reposave)
            add_word = reposave["add_word"]
            del_word = reposave["del_word"]
            file_paths = reposave["file_paths"]
            files = ", ".join(file_paths)
            print(f"Fix in {reponame}: {del_word} -> {add_word} over {files}")
            if suggest_plain:
                submit_plain = get_confirmation("Analysis suggests plain pr, agree?")
            else:
                submit_plain = get_confirmation("Complex repo submit plain pr anyway?")
            context.controller.add(
                {
                    "name": "plain_pr" if submit_plain else "full_pr",
                    "interactive": False,
                    "reponame": reponame,
                    "reposave": reposave,
                }
            )

    return handler


def plain_pr(context):
    """
    Non-interactive task to finish off submission of a pr
    """

    def handler():
        reponame = context.taskjson["reponame"]
        reposave = context.taskjson["reposave"]
        plain_pr_for(reponame, reposave)
        add_cleanup(context, reponame)

    return handler


def full_pr(context):
    """
    Non-interactive task to finish off submission of a pr
    """

    def handler():
        reponame = context.taskjson["reponame"]
        reposave = context.taskjson["reposave"]
        full_pr_for(reponame, reposave)
        add_cleanup(context, reponame)

    return handler


def add_cleanup(context, reponame):
    """
    Kick off cleanup on completion
    """
    context.controller.add(
        {"name": "cleanup", "interactive": True, "priority": 20, "reponame": reponame}
    )


def fast_prepare_a_pr_or_issue_for(reponame, reposave):
    """
    Display a suggestion if the repository looks like it wants an issue and a
    pull request or is happy with just a pull request.
    """
    if check_if_plain_pr(reposave):
        plain_pr_for(reponame, reposave)
    else:
        prepare_a_pr_or_issue_for(reponame, reposave)


def check_if_plain_pr(reposave):
    """
    Display a suggestion if the repository looks like it wants an issue and a
    pull request or is happy with just a pull request.
    """

    repopath = Path(reposave["repodir"])
    suggest_issue = False
    if display_and_check_files(repopath / ".github" / "ISSUE_TEMPLATE"):
        suggest_issue = True
    if display_and_check_files(repopath / ".github" / "pull_request_template.md"):
        suggest_issue = True
    if display_and_check_files(repopath / "CONTRIBUTING.md"):
        suggest_issue = True
    if not suggest_issue:
        return True
    return False


def plain_pr_for(reponame, reposave):
    """
    Create and submit the standard PR.
    """
    make_a_commit(reponame, reposave, False)
    non_interactive_submit_commit(reponame, reposave)


def full_pr_for(reponame, reposave):
    """
    Create and submit the standard PR.
    """
    make_issue(reponame, reposave, True)
    submit_issue(reponame, reposave, None)
    non_interactive_submit_commit(reponame, reposave)


def prepare_a_pr_or_issue_for(reponame, reposave):
    """
    Access repository to prepare a change
    """
    try:
        while True:
            repodir = reposave["repodir"]
            repodirpath = Path(repodir)
            choices = get_pr_or_issue_choices(reponame, repodirpath)
            option = make_choice(choices)
            if option is None:
                return
            handler, context = option
            handler(reponame, reposave, context)
    except UserCancel:
        print("quit - returning to main process")


def get_pr_or_issue_choices(reponame, repodirpath):  # pylint: disable=too-many-locals
    """
    Work out the choices menu for pr/issue
    """
    issue_template = Path(".github") / "ISSUE_TEMPLATE"
    pr_template = Path(".github") / "pull_request_template.md"
    contrib_guide = Path("CONTRIBUTING.md")
    issue = Path("__issue__.txt")
    commit = Path("__commit__.txt")
    prpath = Path("__pr__.txt")
    no_issues = Path("__no_issues__.txt")
    choices = {}
    paths = (
        issue_template,
        pr_template,
        contrib_guide,
        prpath,
        issue,
        commit,
        no_issues,
    )
    for path in paths:
        has_path = (repodirpath / path).exists()
        print(f"{reponame} {'HAS' if has_path else 'does not have'}" f" {path}")
        if has_path:
            choices[f"show {path}"] = (show_path, path)
    choices["make a commit"] = (make_a_commit, False)
    choices["make a full issue"] = (make_issue, True)
    choices["make a short issue"] = (make_issue, False)
    has_issue = (repodirpath / issue).exists()
    if has_issue:
        choices["submit issue"] = (submit_issue, None)
    has_commit = (repodirpath / commit).exists()
    if has_commit:
        choices["submit commit"] = (submit_commit, None)
        choices["submit issue"] = (submit_issue, None)
    return choices


def make_issue(reponame, reposave, is_full):  # pylint: disable=unused-argument
    """
    Prepare an issue template file
    """
    add_word = reposave["add_word"]
    del_word = reposave["del_word"]
    file_paths = reposave["file_paths"]
    repodir = Path(reposave["repodir"])
    files = ", ".join(file_paths)
    title = f"Fix simple typo: {del_word} -> {add_word}"
    if is_full:
        body = f"""\
# Issue Type

[x] Bug (Typo)

# Steps to Replicate

1. Examine {files}.
2. Search for `{del_word}`.

# Expected Behaviour

1. Should read `{add_word}`.
"""
    else:
        body = f"""\
There is a small typo in {files}.
Should read `{add_word}` rather than `{del_word}`.
"""
    with io.open(str(repodir / "__issue__.txt"), "w", encoding="utf-8") as fobj:
        print(title, file=fobj)
        print("", file=fobj)
        print(body, file=fobj)


def make_a_commit(reponame, reposave, is_full):  # pylint: disable=unused-argument
    """
    Prepare a commit template file
    """
    add_word = reposave["add_word"]
    del_word = reposave["del_word"]
    file_paths = reposave["file_paths"]
    repodir = Path(reposave["repodir"])
    files = ", ".join(file_paths)
    commit_path = str(repodir / "__commit__.txt")
    with io.open(commit_path, "w", encoding="utf-8") as fobj:
        print(
            f"""\
docs: Fix simple typo, {del_word} -> {add_word}

There is a small typo in {files}.

Should read `{add_word}` rather than `{del_word}`.
""",
            file=fobj,
        )


def submit_issue(reponame, reposave, ctxt):  # pylint: disable=unused-argument
    """
    Push up an issue
    """
    repodir = Path(reposave["repodir"])
    add_word = reposave["add_word"]
    del_word = reposave["del_word"]
    file_paths = reposave["file_paths"]
    files = ", ".join(file_paths)
    issue_path = str(repodir / "__issue__.txt")
    title, body = load_commit_like_file(issue_path)
    issue_num = issue_via_api(reponame, title, body)
    commit_path = str(repodir / "__commit__.txt")
    with io.open(commit_path, "w", encoding="utf-8") as fobj:
        print(
            f"""\
docs: Fix simple typo, {del_word} -> {add_word}

There is a small typo in {files}.

Closes #{issue_num}
""",
            file=fobj,
        )


def issue_via_api(reponame, title, body):
    """
    Create an issue via the API
    """
    repo = get_parent_repo(reponame)
    issue = repo.create_issue(title=title, body=body)
    return issue.number


def load_commit_like_file(path):
    """
    Read title and body from a well formatted git commit
    """
    with io.open(path, "r", encoding="utf-8") as fobj:
        title = fobj.readline().strip()
        blankline = fobj.readline().strip()
        if blankline != "":
            raise Exception(f"Needs to be a blank second line for {path}.")
        body = fobj.read()
    return title, body


def submit_commit(reponame, reposave, ctxt):  # pylint: disable=unused-argument
    """
    Push up a commit and show message
    """
    print(non_interactive_submit_commit(reponame, reposave))


def non_interactive_submit_commit(reponame, reposave):
    """
    Push up a commit
    """
    try:
        repodir = Path(reposave["repodir"])
        add_word = reposave["add_word"]
        commit_path = str(repodir / "__commit__.txt")
        title, body = load_commit_like_file(commit_path)
        from_branch, to_branch = push_commit(repodir, add_word)
        pullreq = create_pr(reponame, title, body, from_branch, to_branch)
        return f"Created PR #{pullreq.number} view at {pullreq.html_url}"
    except ProcessExecutionError:
        return f"Failed to commit for {reponame}."
    except GithubException:
        return f"Failed to create pr for {reponame}."


def push_commit(repodir, add_word):
    """
    Create commit and push
    """
    git = local["git"]
    with local.cwd(repodir):
        to_branch = git("symbolic-ref", "--short", "HEAD").strip()
        from_branch = f"bugfix_typo_{add_word.replace(' ', '_')}"
        git("commit", "-F", "__commit__.txt")
        git("push", "origin", f"{to_branch}:{from_branch}")
    return from_branch, to_branch


def show_path(reponame, reposave, path):  # pylint: disable=unused-argument
    """
    Display the issue template directory
    """
    print("Opening editor")
    editor = local[get_editor()]
    repodir = reposave["repodir"]
    with local.cwd(repodir):
        _ = editor[str(path)] & FG


def add_change_for_repo(repodir):
    """
    Work out the staged commit and prepare an issue and pull request based on
    the change
    """
    del_word, add_word, file_paths = get_typo(repodir)
    print(f"Changing {del_word} to {add_word} in {', '.join(file_paths)}")
    option = make_simple_choice(["save"], "Do you want to save?")
    if option == "save":
        add_repo_save(repodir, add_word, del_word, file_paths)


def get_typo(repodir):
    """
    Look in the staged commit for the typo.
    """
    git = local["git"]
    del_lines = []
    add_lines = []
    file_paths = []
    with local.cwd(repodir):
        output = git("diff", "--staged")
        for line in output.splitlines():
            if line.startswith("--- a/"):
                index = len("--- a/")
                file_path = line[index:]
                file_paths.append(file_path)
        for line in output.splitlines():
            if line.startswith("-") and not line.startswith("--- "):
                del_lines.append(line[1:])
            elif line.startswith("+") and not line.startswith("+++ "):
                add_lines.append(line[1:])
    if not del_lines or not add_lines:
        print("Could not read diff", file=sys.stderr)
        raise ProcessingFailed()
    del_words = re.findall("[a-zA-Z]+", del_lines[0])
    add_words = re.findall("[a-zA-Z]+", add_lines[0])
    for del_word, add_word in zip(del_words, add_words):
        if del_word != add_word:
            return del_word, add_word, file_paths
    print("Could not locate typo", file=sys.stderr)
    raise ProcessingFailed()
