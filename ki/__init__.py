#!/usr/bin/env python3
"""
Python package `ki` is a command-line interface for the version control and
editing of ``.anki2`` collections as git repositories of markdown files.
Rather than providing an interactive UI like the Anki desktop client, `ki` aims
to allow natural editing *in the filesystem*.

In general, the purpose of `ki` is to allow users to work on large, complex
Anki decks in exactly the same way they work on large, complex software
projects.
.. include:: ./DOCUMENTATION.md
"""

# pylint: disable=invalid-name, missing-class-docstring, broad-except
# pylint: disable=too-many-return-statements, too-many-lines

import os
import re
import json
import shutil
import logging
import hashlib
import sqlite3
import tempfile
import functools
import subprocess
import unicodedata
import configparser
from enum import Enum
from pathlib import Path
from dataclasses import dataclass

import anki
import click
import prettyprinter as pp
from tqdm import tqdm
from lark import Lark
from loguru import logger
from result import Result, Err, Ok, OkErr

import git
from git.exc import GitCommandError

from apy.anki import Anki
from apy.convert import markdown_to_html, plain_to_html

from beartype import beartype
from beartype.typing import (
    Set,
    List,
    Dict,
    Any,
    Optional,
    Union,
    Tuple,
    Generator,
    Callable,
)

from ki.safe import safe
from ki.note import KiNote
from ki.transformer import NoteTransformer, FlatNote

logging.basicConfig(level=logging.INFO)

FieldDict = Dict[str, Any]
NotetypeDict = Dict[str, Any]
TemplateDict = Dict[str, Union[str, int, None]]

# Type alias for OkErr types. Subscript indicates the Ok type.
Res = List

FS_ROOT = Path("/")

GIT = ".git"
GITIGNORE_FILE = ".gitignore"
GITMODULES_FILE = ".gitmodules"

KI = ".ki"
CONFIG_FILE = "config"
HASHES_FILE = "hashes"
BACKUPS_DIR = "backups"
LAST_PUSH_FILE = "last_push"
NO_SM_DIR = "no_submodules_tree"

MODELS_FILE = "models.json"

BATCH_SIZE = 500
HTML_REGEX = r"</?\s*[a-z-][^>]*\s*>|(\&(?:[\w\d]+|#\d+|#x[a-f\d]+);)"
REMOTE_NAME = "anki"
BRANCH_NAME = "main"
CHANGE_TYPES = "A D R M T".split()
TQDM_NUM_COLS = 70
MAX_FIELNAME_LEN = 30
HINT = (
    "hint: Updates were rejected because the tip of your current branch is behind\n"
    + "hint: the Anki remote collection. Integrate the remote changes (e.g.\n"
    + "hint: 'ki pull ...') before pushing again."
)
IGNORE = [GIT, KI, GITIGNORE_FILE, GITMODULES_FILE, MODELS_FILE]
LOCAL_SUFFIX = Path("ki/local")
STAGE_SUFFIX = Path("ki/stage")
REMOTE_SUFFIX = Path("ki/remote")
DELETED_SUFFIX = Path("ki/deleted")
FIELD_HTML_SUFFIX = Path("ki/fieldhtml")

REMOTE_CONFIG_SECTION = "remote"
COLLECTION_FILE_PATH_CONFIG_FIELD = "path"


# TYPES


class ExtantFile(type(Path())):
    """UNSAFE: Indicates that file *was* extant when it was resolved."""


class ExtantDir(type(Path())):
    """UNSAFE: Indicates that dir *was* extant when it was resolved."""


class EmptyDir(ExtantDir):
    """UNSAFE: Indicates that dir *was* empty (and extant) when it was resolved."""


class NoPath(type(Path())):
    """UNSAFE: Indicates that path *was not* extant when it was resolved."""


class Singleton(type(Path())):
    """UNSAFE: A path consisting of a single component (e.g. `file`, not `dir/file`)."""


class ExtantStrangePath(type(Path())):
    """
    UNSAFE: Indicates that path was extant but weird (e.g. a device or socket)
    when it was resolved.
    """


# EXCEPTIONS


class ExpectedFileButGotDirectoryError(Exception):
    pass


class ExpectedDirectoryButGotFileError(Exception):
    pass


class ExpectedEmptyDirectoryButGotNonEmptyDirectoryError(Exception):
    pass


class StrangeExtantPathError(Exception):
    pass


class NotKiRepoError(Exception):
    @beartype
    def __init__(self):
        msg = "fatal: not a ki repository (or any parent up to mount point /)\n"
        msg += "Stopping at filesystem boundary."
        super().__init__(msg)


class UpdatesRejectedError(Exception):
    @beartype
    def __init__(self, col_file: ExtantFile):
        msg = f"Failed to push some refs to '{col_file}'\n{HINT}"
        super().__init__(msg)


class TargetExistsError(Exception):
    @beartype
    def __init__(self, target: Path):
        msg = f"fatal: destination path '{target}' already exists and is "
        msg += "not an empty directory."
        super().__init__(msg)


class GitRefNotFoundError(Exception):
    @beartype
    def __init__(self, repo: git.Repo, sha: str):
        msg = f"Repo at '{repo.working_dir}' doesn't contain ref '{sha}'"
        super().__init__(msg)


class CollectionChecksumError(Exception):
    @beartype
    def __init__(self, col_file: ExtantFile):
        msg = f"Checksum mismatch on {col_file}. Was file changed?"
        super().__init__(msg)


# ENUMS


class GitChangeType(Enum):
    """Enum for git file change types."""

    ADDED = "A"
    DELETED = "D"
    RENAMED = "R"
    MODIFIED = "M"
    TYPECHANGED = "T"


# DATACLASSES


@beartype
@dataclass(frozen=True)
class Delta:
    """The git delta for a single file."""

    status: GitChangeType
    path: ExtantFile
    relpath: Path


@beartype
@dataclass(frozen=True)
class KiRepo:
    """
    UNSAFE: A ki repository, including:
    - .ki/hashes
    - .ki/config

    Existence of collection path is guaranteed.
    """

    repo: git.Repo
    root: ExtantDir
    ki_dir: ExtantDir
    col_file: ExtantFile
    backups_dir: ExtantDir
    config_file: ExtantFile
    hashes_file: ExtantFile
    models_file: ExtantFile
    last_push_file: ExtantFile
    no_modules_repo: git.Repo


@beartype
@dataclass(frozen=True)
class KiRepoRef:
    """
    UNSAFE: A repo-commit pair, where ``sha`` is guaranteed to be an extant
    commit hash of ``repo``.
    """

    kirepo: KiRepo
    sha: str


@beartype
@dataclass(frozen=True)
class RepoRef:
    """
    UNSAFE: A repo-commit pair, where ``sha`` is guaranteed to be an extant
    commit hash of ``repo``.
    """

    repo: git.Repo
    sha: str


@beartype
@dataclass(frozen=True)
class Leaves:
    root: ExtantDir
    files: Dict[str, ExtantFile]
    dirs: Dict[str, EmptyDir]


# MAYBES


@safe
@beartype
def M_nopath(path: Path) -> Result[NoPath, Exception]:
    """
    Maybe convert a path to a NoPath, i.e. a path that did not exist at
    resolve-time, which is when this function was called.
    """
    path = path.resolve()
    if path.exists():
        return Err(FileExistsError(str(path)))
    return Ok(NoPath(path))


@safe
@beartype
def M_xfile(path: Path) -> Result[ExtantFile, Exception]:
    """
    Attempt to instantiate an ExtantFile.
    """
    # Resolve path.
    path = path.resolve()

    # Check that path exists and is a file.
    if not path.exists():
        return Err(FileNotFoundError(str(path)))
    if path.is_dir():
        return Err(ExpectedFileButGotDirectoryError(str(path)))
    if not path.is_file():
        return Err(StrangeExtantPathError(str(path)))

    # Must be an extant file.
    return Ok(ExtantFile(path))


@safe
@beartype
def M_xdir(path: Path) -> Result[ExtantDir, Exception]:
    """
    Attempt to instantiate an ExtantDir.
    """
    # Resolve path.
    path = path.resolve()

    # Check that path exists and is a directory.
    if not path.exists():
        return Err(FileNotFoundError(str(path)))
    if path.is_dir():
        return Ok(ExtantDir(path))
    if path.is_file():
        return Err(ExpectedDirectoryButGotFileError(str(path)))
    return Err(StrangeExtantPathError(str(path)))


@safe
@beartype
def M_emptydir(path: Path) -> Result[ExtantDir, Exception]:
    """
    Attempt to instantiate an ExtantDir.
    """
    # Check if it's an extant directory.
    res: OkErr = M_xdir(path)

    # Return the Err if not.
    if res.is_err():
        return res

    # Unwrap the value otherwise.
    directory: ExtantDir = res.unwrap()
    if is_empty(directory):
        return Ok(EmptyDir(Path(directory)))
    return ExpectedEmptyDirectoryButGotNonEmptyDirectoryError(str(directory))


@safe
@beartype
def M_repo(root: ExtantDir) -> Result[git.Repo, Exception]:
    """Read a git repo safely."""
    try:
        repo = git.Repo(root)
    except Exception as err:
        return Err(err)
    return Ok(repo)


@safe
@beartype
def M_kirepo(cwd: ExtantDir) -> Result[KiRepo, Exception]:
    """Get the containing ki repository of ``path``."""
    current = cwd

    # Note that ``current`` is an ``ExtantDir`` but ``FS_ROOT`` is a ``Path``.
    # We can make this comparison because both are instances of ``Path`` and
    # ``Path`` implements the ``==`` operator nicely.
    while current != FS_ROOT:
        ki_dir = fftest(current / KI)
        if isinstance(ki_dir, ExtantDir):
            break
        current = fparent(current)

    if current == FS_ROOT:
        return Err(NotKiRepoError())

    # Root directory and ki directory of repo now guaranteed to exist.
    root = current
    repo: OkErr = M_repo(root)

    # Check that relevant files in .ki/ subdirectory exist.
    backups_dir = M_xdir(ki_dir / BACKUPS_DIR)
    config_file = M_xfile(ki_dir / CONFIG_FILE)
    hashes_file = M_xfile(ki_dir / HASHES_FILE)
    models_file = M_xfile(root / MODELS_FILE)
    last_push_file = M_xfile(ki_dir / LAST_PUSH_FILE)

    # Load the no_submodules_tree.
    no_modules_dir = M_xdir(ki_dir / NO_SM_DIR)
    no_modules_repo = M_repo(no_modules_dir)

    # Check that collection file exists.
    col_file = parse_config_file(config_file)

    @safe
    @beartype
    def constructor(
        repo: git.Repo,
        root: ExtantDir,
        ki_dir: ExtantDir,
        col_file: ExtantFile,
        backups_dir: ExtantDir,
        config_file: ExtantFile,
        hashes_file: ExtantFile,
        models_file: ExtantFile,
        last_push_file: ExtantFile,
        no_modules_repo: git.Repo,
    ) -> Result[KiRepo, Exception]:
        return KiRepo(
            repo,
            root,
            ki_dir,
            col_file,
            backups_dir,
            config_file,
            hashes_file,
            models_file,
            last_push_file,
            no_modules_repo,
        )

    return constructor(
        repo,
        root,
        ki_dir,
        col_file,
        backups_dir,
        config_file,
        hashes_file,
        models_file,
        last_push_file,
        no_modules_repo,
    )


@safe
@beartype
def M_kirepo_ref(kirepo: KiRepo, sha: str) -> Result[KiRepoRef, Exception]:
    if not ref_exists(kirepo.repo, sha):
        return Err(GitRefNotFoundError(kirepo.repo, sha))
    return Ok(KiRepoRef(kirepo, sha))


@safe
@beartype
def M_repo_ref(repo: git.Repo, sha: str) -> Result[RepoRef, Exception]:
    if not ref_exists(repo, sha):
        return Err(GitRefNotFoundError(repo, sha))
    return Ok(RepoRef(repo, sha))


@safe
@beartype
def M_head_repo_ref(repo: git.Repo) -> Result[RepoRef, Exception]:
    # GitPython raises a ValueError when references don't exist.
    try:
        ref = RepoRef(repo, repo.head.commit.hexsha)
    except ValueError as err:
        return Err(err)
    return Ok(ref)


@safe
@beartype
def M_head_kirepo_ref(kirepo: KiRepo) -> Result[KiRepoRef, Exception]:
    # GitPython raises a ValueError when references don't exist.
    try:
        ref = KiRepoRef(kirepo, ki.repo.repo.head.commit.hexsha)
    except ValueError as err:
        return Err(err)
    return Ok(ref)


# DANGER


@safe
@beartype
def rmtree(target: ExtantDir) -> Result[NoPath, Exception]:
    """Call shutil.rmtree()."""
    shutil.rmtree(target)
    return M_nopath(target)


@safe
@beartype
def copytree(source: ExtantDir, target: NoPath) -> Result[ExtantDir, Exception]:
    """Call shutil.copytree()."""
    shutil.copytree(source, target)
    return M_xdir(target)


@beartype
def fcwd() -> ExtantDir:
    """Call Path.cwd()."""
    return ExtantDir(Path.cwd())


@beartype
def fftest(
    path: Path,
) -> Union[ExtantFile, ExtantDir, EmptyDir, ExtantStrangePath, NoPath]:
    """
    Fast ftest, where the f just means function.

    Test whether ``path`` is a file, a directory, or something else. If
    something else, we consider that, for the sake of simplicity, a
    NoPath.
    """
    if path.is_file():
        return ExtantFile(path)
    if path.is_dir():
        if is_empty(path):
            return EmptyDir(path)
        return ExtantDir(path)
    if path.exists():
        return ExtantStrangePath(path)
    return NoPath(path)


@safe
@beartype
def ftouch(directory: ExtantDir, name: str) -> ExtantFile:
    """Touch a file."""
    path = directory / singleton(name)
    path.touch()
    return ExtantFile(path)


@safe
@beartype
def fmkleaves(
    root: EmptyDir,
    *,
    files: Optional[Dict[str, str]] = None,
    dirs: Optional[Dict[str, str]] = None,
) -> Result[Leaves, Exception]:
    """Safely populate an empty directory with empty files and empty subdirectories."""
    new_files: Dict[str, ExtantFile] = {}
    new_dirs: Dict[str, EmptyDir] = {}
    if files is not None:
        for key, token in files.items():
            new_files[key] = ftouch(root, token)
    if dirs is not None:
        for key, token in dirs.items():
            new_dirs[key] = ffmksubdir(root, singleton(token))
    return Ok(Leaves(M_xdir(root), new_files, dirs))


@safe
@beartype
def fmkdir(path: NoPath) -> Result[EmptyDir, Exception]:
    """Make a directory (with parents)."""
    path.mkdir(parents=True)
    return M_emptydir(path)


@beartype
def ffmksubdir(directory: EmptyDir, suffix: Path) -> EmptyDir:
    """
    Make a subdirectory of an empty directory (with parents).

    Returns
    -------
    EmptyDir
        The created subdirectory.
    """
    subdir = directory / suffix
    subdir.mkdir(parents=True)
    directory.__class__ = ExtantDir
    return EmptyDir(subdir)


@safe
@beartype
def fforce_mkdir(path: Path) -> Result[ExtantDir, Exception]:
    """Make a directory (with parents, ok if it already exists)."""
    path.mkdir(parents=True, exist_ok=True)
    return M_xdir(path)


@beartype
def fchdir(directory: ExtantDir) -> ExtantDir:
    """Changes working directory and returns old cwd."""
    old: ExtantDir = fcwd()
    os.chdir(directory)
    return old


@safe
@beartype
def fparent(
    path: Union[ExtantFile, ExtantDir]
) -> Result[Union[ExtantFile, ExtantDir], Exception]:
    """Get the parent of a path that exists."""
    if path.resolve() == FS_ROOT:
        return M_xdir(FS_ROOT)
    if isinstance(path, ExtantFile):
        return M_xdir(path.parent)
    return M_xdir(path.parent)


@beartype
def ffmkdtemp() -> EmptyDir:
    """Make a temporary directory (in /tmp)."""
    return EmptyDir(tempfile.mkdtemp())


@beartype
def ffcopyfile(source: ExtantFile, target: Path, target_root: ExtantDir) -> ExtantFile:
    """Force copy a file (potentially overwrites the target path)."""
    shutil.copyfile(source, target)
    return ExtantFile(target)


@safe
@beartype
def fmkdempty(path: Path) -> Result[EmptyDir, Exception]:
    """
    Make an empty directory out of ``path``. If the directory already exists,
    we return an exception unless it is empty.
    """
    path = fftest(path)
    if isinstance(path, NoPath):
        return fmkdir(path)
    if isinstance(path, EmptyDir):
        return Ok(path)
    return Err(TargetExistsError(path))


@beartype
def frglob(root: ExtantDir, pattern: str) -> List[ExtantFile]:
    """Call root.rglob() and returns only files."""
    files = filter(
        lambda p: isinstance(p, ExtantFile), map(fftest, root.rglob(pattern))
    )
    return list(files)


@safe
@beartype
def fcreate_local_remote(
    repo: git.Repo, target: git.Repo, name: str
) -> Result[git.Remote, Exception]:
    """
    Create a remote for ``repo`` pointing to the local git repository
    ``target`` called ``name``.
    """
    # TODO: Can this fail? What if the remote name already exists?
    return repo.create_remote(name, target.git_dir)


@beartype
def is_empty(directory: ExtantDir) -> bool:
    """Check if directory is empty, quickly."""
    return not next(os.scandir(directory), None)


@safe
@beartype
def parse_config_file(config_file: ExtantFile) -> Result[ExtantFile, Exception]:
    """
    Parse the .ki/config file for the path to the .anki2 collection file
    associated with this repository. Return this path.
    """
    # Parse config file.
    config = configparser.ConfigParser()
    config.read(config_file)
    col_file = Path(config[REMOTE_CONFIG_SECTION][COLLECTION_FILE_PATH_CONFIG_FIELD])
    return M_xfile(col_file)


@safe
@beartype
def join(path_1: Path, path_2: Path) -> Result[Path, Exception]:
    return Ok(path_1 / path_2)


@safe
@beartype
def working_dir(repo: git.Repo) -> Result[ExtantDir, Exception]:
    """Get working directory of a repo."""
    return M_xdir(Path(repo.working_dir))


@safe
@beartype
def git_dir(repo: git.Repo) -> Result[ExtantDir, Exception]:
    """Get git directory of a repo."""
    return M_xdir(Path(repo.git_dir))


# SAFE


@beartype
def singleton(name: str) -> Singleton:
    """Removes all forward slashes and returns a Singleton pathlib.Path."""
    return Singleton(name.replace("/", ""))


# UNSAFE: Should catch exception and transform into nice Err that tells user what to do.
@beartype
def lock(kirepo: KiRepo) -> sqlite3.Connection:
    """Acquire a lock on a SQLite3 database given a path."""
    con = sqlite3.connect(kirepo.col_file)
    con.isolation_level = "EXCLUSIVE"
    con.execute("BEGIN EXCLUSIVE")
    return con


@beartype
def unlock(con: sqlite3.Connection) -> bool:
    """Unlock a SQLite3 database."""
    con.commit()
    con.close()
    return True


@beartype
def md5(path: ExtantFile) -> str:
    """Compute md5sum of file at `path`."""
    hash_md5 = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


@beartype
def ref_exists(repo: git.Repo, ref: str) -> bool:
    """Check if git commit reference exists in repository."""
    try:
        repo.git.rev_parse("--verify", ref)
    except GitCommandError:
        return False
    return True


@safe
@beartype
def get_ephemeral_repo(
    suffix: Path, repo_ref: RepoRef, md5sum: str
) -> Result[git.Repo, Exception]:
    """Get a temporary copy of a git repository in /tmp/<suffix>/."""
    tempdir: EmptyDir = ffmkdtemp()
    root: EmptyDir = ffmksubdir(tempdir, suffix)

    # Git clone `repo` at latest commit in `/tmp/.../<suffix>/<md5sum>`.
    repo: git.Repo = repo_ref.repo
    branch = repo.active_branch
    target: Path = root / md5sum

    # UNSAFE: But only called here, and it should be guaranteed to work because
    # ``repo`` is actually a git repository, presumably there is always an
    # active branch, and ``target`` does not exist.
    ephem = git.Repo.clone_from(repo.working_dir, target, branch=branch, recursive=True)

    # Do a reset --hard to the given SHA.
    ephem.git.reset(repo_ref.sha, hard=True)

    return Ok(ephem)


@safe
@beartype
def get_ephemeral_kirepo(
    suffix: Path, kirepo_ref: KiRepoRef, md5sum: str
) -> Result[KiRepo, Exception]:
    """
    Given a KiRepoRef, i.e. a pair of the form (kirepo, SHA), we clone
    ``kirepo.repo`` into a temp directory and hard reset to the given commit
    hash.

    Parameters
    ----------
    suffix : pathlib.Path
        /tmp/.../ path suffix, e.g. ``ki/local/``.
    kirepo_ref : KiRepoRef
        The ki repository to clone, and a commit for it.
    md5sum : str
        The md5sum of the associated anki collection.

    Returns
    -------
    KiRepo
        The cloned repository.
    """
    ref: Res[RepoRef] = M_repo_ref(kirepo_ref.kirepo.repo, kirepo_ref.sha)
    ephem: Res[git.Repo] = get_ephemeral_repo(suffix, ref, md5sum)
    ephem_ki_dir: Res[NoPath] = M_nopath(join(working_dir(ephem), KI))

    # Functions that don't return anything used in the next statement must have
    # their results caught and optionally returned.
    res: OkErr = copytree(kirepo_ref.kirepo.ki_dir, ephem_ki_dir)
    if res.is_err():
        return res
    kirepo: Res[KiRepo] = M_kirepo(working_dir(ephem))

    return kirepo


@beartype
def is_anki_note(path: ExtantFile) -> bool:
    """Check if file is an `apy`-style markdown anki note."""
    path = str(path)

    # Ought to have markdown file extension.
    if path[-3:] != ".md":
        return False
    with open(path, "r", encoding="UTF-8") as md_f:
        lines = md_f.readlines()
    if len(lines) < 2:
        return False
    if lines[0] != "## Note\n":
        return False
    if not re.match(r"^nid: [0-9]+$", lines[1]):
        return False
    return True


@beartype
def path_ignore_fn(path: Path, patterns: List[str], root: ExtantDir) -> bool:
    """Lambda to be used as first argument to filter(). Filters out paths-to-ignore."""
    for p in patterns:
        if p == path.name:
            logger.warning(f"Ignoring {path} matching pattern {p}")
            return False

    # Ignore files that match a pattern in ``patterns`` ('*' not supported).
    for ignore_path in [root / p for p in patterns]:
        parents = [path.resolve()] + [p.resolve() for p in path.parents]
        if ignore_path.resolve() in parents:
            logger.warning(f"Ignoring {path} matching pattern {ignore_path}")
            return False

    # If ``path`` is an extant file (not a directory) and NOT a note, ignore it.
    # UNSAFE STUFF!
    if path.exists() and path.resolve().is_file():
        file = ExtantFile(path)
        if not is_anki_note(file):
            logger.warning(f"Not Anki note {file}")
            return False

    return True


@beartype
def unsubmodule_repo(repo: git.Repo) -> None:
    """
    Un-submodule all the git submodules (convert to ordinary subdirectories and
    destroy commit history).

    MUTATES REPO in-place!

    UNSAFE: git.rm() calls.
    """
    gitmodules_path: Path = Path(repo.working_dir) / GITMODULES_FILE
    for sm in repo.submodules:

        # Untrack, remove gitmodules file, remove .git file, and add directory back.
        sm.update()

        # Guaranteed to exist by gitpython.
        sm_path = Path(sm.module().working_tree_dir)
        repo.git.rm(sm_path, cached=True)

        # May not exist.
        repo.git.rm(gitmodules_path)

        # Guaranteed to exist by gitpython, and safe because we pass
        # ``missing_ok=True``, which means no error is raised.
        (sm_path / GIT).unlink(missing_ok=True)

        # Should still exist after git.rm().
        repo.git.add(sm_path)
        _ = repo.index.commit(f"Add submodule {sm.name} as ordinary directory.")

    if gitmodules_path.exists():
        repo.git.rm(gitmodules_path)
        _ = repo.index.commit("Remove '.gitmodules' file.")


@safe
@beartype
def get_deltas_since_last_push(
    ref: RepoRef,
    md5sum: str,
    ignore_fn: Callable[[Path], bool],
    parser: Lark,
    transformer: NoteTransformer,
) -> Result[List[Delta], Exception]:
    """
    Get the list of all deltas (changed files) since the last time that ``ki
    push`` was run successfully.

    In order to treat submodules, we keep a separate version of the entire
    repository in ``.ki/no_submodules_tree``. This is itself a git submodule.
    It is NOT a kirepo, because it does not have a .ki directory of its own.

    We need only commit to it during push calls, so there is no need to make
    any adjustments to ``_clone()``.

    Parameters
    ----------
    """

    a_repo: Res[git.Repo] = get_ephemeral_repo(DELETED_SUFFIX, ref, md5sum)
    b_repo: git.Repo = ref.repo
    return diff_repos(a_repo, b_repo, ref, ignore_fn, parser, transformer)


@safe
@beartype
def diff_repos(
    a_repo: git.Repo,
    b_repo: git.Repo,
    ref: RepoRef,
    ignore_fn: Callable[[Path], bool],
    parser: Lark,
    transformer: NoteTransformer,
) -> Result[List[Delta], Exception]:
    # Use a `DiffIndex` to get the changed files.
    deltas = []
    a_dir = Path(a_repo.working_dir)
    b_dir = Path(b_repo.working_dir)
    diff_index = b_repo.commit(ref.sha).diff(b_repo.head.commit)
    for change_type in GitChangeType:
        for diff in diff_index.iter_change_type(change_type.value):

            if not ignore_fn(a_dir / diff.a_path) or not ignore_fn(a_dir / diff.b_path):
                logger.warning(f"Ignoring:\n{diff.a_path}\n{diff.b_path}")
                continue

            a_path = fftest(a_dir / diff.a_path)
            b_path = fftest(b_dir / diff.b_path)

            a_relpath = Path(diff.a_path)
            b_relpath = Path(diff.b_path)

            if change_type == GitChangeType.DELETED:
                if not isinstance(a_path, ExtantFile):
                    logger.warning(f"Deleted file not found in source commit: {a_path}")
                    continue

                deltas.append(Delta(change_type, a_path, a_relpath))
                continue

            if not isinstance(b_path, ExtantFile):
                logger.warning(f"Diff target not found: {b_path}")
                continue

            if change_type == GitChangeType.RENAMED:
                a_flatnote: FlatNote = parse_markdown_note(parser, transformer, a_path)
                b_flatnote: FlatNote = parse_markdown_note(parser, transformer, b_path)
                if a_flatnote.nid != b_flatnote.nid:
                    deltas.append(Delta(GitChangeType.DELETED, a_path, a_relpath))
                    deltas.append(Delta(GitChangeType.ADDED, b_path, b_relpath))

    return deltas


@safe
@beartype
def get_models_recursively(kirepo: KiRepo) -> Res[Dict[int, NotetypeDict], Exception]:
    """Find and merge all ``models.json`` files recursively."""
    all_new_models: Dict[int, NotetypeDict] = {}

    # Load notetypes from json files.
    for models_file in frglob(kirepo.root, MODELS_FILE):
        with open(models_file, "r", encoding="UTF-8") as models_f:
            new_models: Dict[int, NotetypeDict] = json.load(models_f)

        # Add mappings to dictionary.
        all_new_models.update(new_models)

    return all_new_models


@beartype
def display_fields_health_warning(note: anki.notes.Note) -> int:
    """Display warnings when Anki's fields health check fails."""
    health = note.fields_check()
    if health == 1:
        logger.warning(f"Found empty note:\n {note}")
        logger.warning(f"Fields health check code: {health}")
    elif health == 2:
        logger.warning(f"\nFound duplicate note when adding new note w/ nid {note.id}.")
        logger.warning(f"Notetype/fields of note {note.id} match existing note.")
        logger.warning("Note was not added to collection!")
        logger.warning(f"First field: {note.fields[0]}")
        logger.warning(f"Fields health check code: {health}")
    elif health != 0:
        logger.error(f"Failed to process note '{note.id}'.")
        logger.error(f"Note failed fields check with unknown error code: {health}")
    return health


# UNREFACTORED


@beartype
def parse_markdown_note(
    parser: Lark, transformer: NoteTransformer, notes_file: ExtantFile
) -> FlatNote:
    """Parse with lark."""
    tree = parser.parse(notes_file.read_text(encoding="UTF-8"))
    flatnotes: List[FlatNote] = transformer.transform(tree)

    # UNSAFE!
    return flatnotes[0]


# TODO: Refactor into a safe function.
@beartype
def update_kinote(kinote: KiNote, flatnote: FlatNote) -> None:
    """
    Update an `apy` Note in a collection.

    Currently fails if the model has changed. It does not update the model name
    of the KiNote, and it also assumes that the field names have not changed.
    """
    kinote.n.tags = flatnote.tags

    # TODO: Can this raise an error? What if the deck is not in the collection?
    # Should we create it? Probably.
    kinote.set_deck(flatnote.deck)

    # Get new notetype from collection (None if nonexistent).
    notetype: Optional[NotetypeDict] = kinote.n.col.models.by_name(flatnote.model)

    # If notetype doesn't exist, raise an error.
    if notetype is None:
        msg = f"Notetype '{flatnote.model}' doesn't exist. "
        msg += "Create it in Anki before adding notes via ki."
        raise FileNotFoundError(msg)

    # Validate field keys against notetype.
    old_model = kinote.n.note_type()
    validate_flatnote_fields(old_model, flatnote)

    # Set field values.
    for key, field in flatnote.fields.items():
        if flatnote.markdown:
            kinote.n[key] = markdown_to_html(field)
        else:
            kinote.n[key] = plain_to_html(field)

    # Flush note, mark collection as modified, and display any warnings.
    kinote.n.flush()
    kinote.a.modified = True
    display_fields_health_warning(kinote.n)


# TODO: Refactor into a safe function.
@beartype
def validate_flatnote_fields(
    model: NotetypeDict, flatnote: FlatNote
) -> Optional[Exception]:
    """Validate that the fields given in the note match the notetype."""
    # Set current notetype for collection to `model_name`.
    model_field_names = [field["name"] for field in model["flds"]]

    if len(flatnote.fields.keys()) != len(model_field_names):
        return ValueError(f"Not enough fields for model {flatnote.model}!")

    for x, y in zip(model_field_names, flatnote.fields.keys()):
        if x != y:
            return ValueError("Inconsistent field names " f"({x} != {y})")
    return None


@beartype
def set_model(a: Anki, model_name: str) -> NotetypeDict:
    """Set current model based on model name."""
    current = a.col.models.current(for_deck=False)
    if current["name"] == model_name:
        return current

    model = a.get_model(model_name)
    if model is None:
        # UNSAFE!
        echo(f'Model "{model_name}" was not recognized!')
        raise ValueError

    a.col.models.set_current(model)
    return model


# TODO: Refactor into a safe function.
@beartype
def add_note_from_flatnote(a: Anki, flatnote: FlatNote) -> Optional[KiNote]:
    """Add a note given its FlatNote representation, provided it passes health check."""
    # TODO: Does this assume model exists? Kind of. We abort/raise an error if
    # it doesn't.
    model = set_model(a, flatnote.model)
    validate_flatnote_fields(model, flatnote)

    # Note that we call ``a.set_model(flatnote.model)`` above, so the current
    # model is the model given in ``flatnote``.
    notetype = a.col.models.current(for_deck=False)
    note = a.col.new_note(notetype)

    # Create new deck if deck does not exist.
    note.note_type()["did"] = a.col.decks.id(flatnote.deck)

    if flatnote.markdown:
        note.fields = [markdown_to_html(x) for x in flatnote.fields.values()]
    else:
        note.fields = [plain_to_html(x) for x in flatnote.fields.values()]

    for tag in flatnote.tags:
        note.add_tag(tag)

    health = display_fields_health_warning(note)

    result = None
    if health == 0:
        a.col.addNote(note)
        a.modified = True
        result = KiNote(a, note)

    return result


# TODO: This function is still somewhat unsafe.
@beartype
def get_sort_field_text(
    a: Anki, note: anki.notes.Note, notetype: NotetypeDict
) -> Optional[str]:
    """
    Return the sort field content of a model.

    .field_map() call may raise a KeyError if ``notetype`` doesn't have the
    requisite fields.
    notetype may not have a sortf key
    sort_idx may not be in field_names
    sort_field_name may not be in note
    """

    # Get fieldmap from notetype.
    fieldmap: Dict[str, Tuple[int, FieldDict]]
    try:
        fieldmap = a.col.models.field_map(notetype)
    except KeyError as err:
        logger.warning(err)
        return None

    # Get sort index.
    try:
        sort_idx = a.col.models.sort_idx(notetype)
    except KeyError as err:
        logger.warning(err)
        return None

    # Map field indices to field names.
    sort_field_name: Optional[str] = None
    for field_name, (idx, _) in fieldmap.items():
        if idx == sort_idx:
            sort_field_name = field_name

    if sort_field_name is None:
        return None

    # Get sort field_name.
    try:
        sort_field_text: str = note[sort_field_name]
    except KeyError as err:
        logger.warning(err)
        return None

    return sort_field_text


@beartype
def get_note_path(sort_field_text: str, deck_dir: ExtantDir) -> ExtantFile:
    """Get note path from sort field text."""
    field_text = sort_field_text

    MD = ".md"

    # Construct filename, stripping HTML tags and sanitizing (quickly).
    field_text = plain_to_html(field_text)
    field_text = re.sub("<[^<]+?>", "", field_text)
    name = field_text[:MAX_FIELNAME_LEN]
    name = Path(slugify(name, allow_unicode=True))
    filename = name.with_suffix(MD)
    note_path = fftest(deck_dir / filename)

    i = 1
    while not isinstance(note_path, NoPath):
        filename = Path(f"{name}_{i}").with_suffix(MD)
        note_path = fftest(deck_dir / filename)
        i += 1

    note_path: ExtantFile = ftouch(deck_dir, str(filename))

    return note_path


# TODO: Refactor into a safe function.
@beartype
def backup(col_file: ExtantFile) -> None:
    """Backup collection to `.ki/backups`."""
    md5sum = md5(col_file)
    backupsdir = Path.cwd() / ".ki" / "backups"
    assert not backupsdir.is_file()
    if not backupsdir.is_dir():
        backupsdir.mkdir()
    backup_path = backupsdir / f"{md5sum}.anki2"
    if backup_path.is_file():
        click.secho("Backup already exists.", bold=True)
        return
    assert not backup_path.is_file()
    echo(f"Writing backup of .anki2 file to '{backupsdir}'")
    shutil.copyfile(col_file, backup_path)
    assert backup_path.is_file()


@beartype
def append_md5sum(
    ki_dir: ExtantDir, tag: str, md5sum: str, silent: bool = False
) -> None:
    """Append an md5sum hash to the hashes file."""
    hashes_file = ki_dir / HASHES_FILE
    with open(hashes_file, "a+", encoding="UTF-8") as hashes_f:
        hashes_f.write(f"{md5sum}  {tag}\n")
    echo(f"Wrote md5sum to '{hashes_file}'", silent)
    return Ok()


@beartype
def create_deck_dir(
    deck_name: str, targetdir: ExtantDir
) -> Result[ExtantDir, Exception]:
    """Construct path to deck directory and create it."""
    # Strip leading periods so we don't get hidden folders.
    components = deck_name.split("::")
    components = [re.sub(r"^\.", r"", comp) for comp in components]
    deck_path = Path(targetdir, *components)
    return fforce_mkdir(deck_path)


@beartype
def get_field_note_id(nid: int, fieldname: str) -> str:
    """A str ID that uniquely identifies field-note pairs."""
    return f"{nid}{slugify(fieldname, allow_unicode=True)}"


@beartype
def get_batches(lst: List[ExtantFile], n: int) -> Generator[ExtantFile, None, None]:
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


@beartype
def write_repository(
    col_file: ExtantFile, targetdir: ExtantDir, leaves: Leaves, silent: bool
) -> Result[bool, Exception]:
    """Write notes to appropriate directories in ``targetdir``."""

    # Create config file.
    config_file: ExtantFile = leaves.files[CONFIG_FILE]
    config = configparser.ConfigParser()
    config["remote"] = {"path": col_file}
    with open(config_file, "w", encoding="UTF-8") as config_f:
        config.write(config_f)

    # Create temp directory for htmlfield text files.
    tempdir: EmptyDir = ffmkdtemp()
    root: EmptyDir = ffmksubdir(tempdir, FIELD_HTML_SUFFIX)

    paths: Dict[str, ExtantFile] = {}
    decks: Dict[str, List[KiNote]] = {}

    # Open deck with `apy`, and dump notes and markdown files.
    with Anki(path=col_file) as a:
        all_nids = list(a.col.find_notes(query=""))
        for nid in tqdm(all_nids, ncols=TQDM_NUM_COLS, disable=silent):
            kinote = KiNote(a, a.col.get_note(nid))
            decks[kinote.deck] = decks.get(kinote.deck, []) + [kinote]
            for fieldname, fieldtext in kinote.fields.items():
                if re.search(HTML_REGEX, fieldtext):
                    fid: str = get_field_note_id(nid, fieldname)
                    html_file: ExtantFile = ftouch(root, fid)
                    html_file.write_text(fieldtext, encoding="UTF-8")
                    paths[fid] = html_file

        # TODO: Consider adding a block in ``safe()`` that looks for a token
        # keyword argument, like ``_err``, and bypasses the function call if it
        # is an Err. If it is an Ok, it simply removes that key-value pair from
        # ``kwargs`` and calls the function as it normally would.
        tidied: OkErr = tidy_html_recursively(root, silent)
        wrote: OkErr = write_notes(a, targetdir, decks, paths, tidied)

    # Replace with frmtree.
    shutil.rmtree(root)

    return wrote


@safe
@beartype
def write_notes(
    a: Anki,
    targetdir: ExtantDir,
    decks: Dict[str, List[KiNote]],
    paths: Dict[str, ExtantFile],
    tidied: bool,
) -> Result[bool, Exception]:
    """
    There is a bug in 'write_notes()'. Then sorting of deck names is done by
    length, in reverse, which means we start from the deepest, most specific
    decks, and end up at the root. I.e. We are traversing up a tree from the
    leaves to the root. Previously (see earlier commits), we simply accumulated
    all model ids and wrote the entire list (so far) to each deck's model.json
    file. But this is actually wrong, because if we have two subtrees, the one
    with larger height may have its model ids end up in the other. Since we're
    sorting by string length, it's a very imprecise, wrong way to do things. The
    proper way to do this is a DFS traversal, perhaps recursively, which will
    make it easier to keep things purely functional, accumulating the model ids
    of the children in each node. For this, we must construct a tree from the
    deck names.
    """
    # Write models to disk.
    models_map: Dict[int, NotetypeDict] = {}
    for model in a.col.models.all():
        # UNSAFE!
        model_id: Optional[NotetypeDict] = a.col.models.id_for_name(model["name"])
        models_map[model_id] = model

    with open(targetdir / MODELS_FILE, "w", encoding="UTF-8") as f:
        json.dump(models_map, f, ensure_ascii=False, indent=4)

    for deck_name in sorted(set(decks.keys()), key=len, reverse=True):
        deck_dir: Res[ExtantDir] = create_deck_dir(deck_name, targetdir)
        wrote: OkErr = write_deck(a, decks[deck_name], deck_dir, paths, models_map)
        if wrote.is_err():
            return wrote

    return Ok()


@safe
@beartype
def write_deck(
    a: Anki,
    deck: List[KiNote],
    deck_dir: ExtantDir,
    paths: Dict[str, ExtantFile],
    models_map: Dict[int, NotetypeDict],
) -> Result[bool, Exception]:
    model_ids: Set[int] = set()
    for kinote in deck:

        # Add the notetype id.
        model = kinote.n.note_type()
        if model is None:
            logger.warning(f"Couldn't find notetype for {kinote}")
            continue
        model_id = a.col.models.id_for_name(model["name"])
        model_ids.add(model_id)

        sort_field_text: Optional[str] = get_sort_field_text(a, kinote.n, model)
        if sort_field_text is None:
            logger.warning(f"Couldn't find sort field for {kinote}")
            continue
        notepath: ExtantFile = get_note_path(sort_field_text, deck_dir)
        payload: str = get_tidy_payload(kinote, paths)
        notepath.write_text(payload, encoding="UTF-8")

    # Write ``models.json`` for current deck.
    deck_models_map = {mid: models_map[mid] for mid in model_ids}
    with open(deck_dir / MODELS_FILE, "w", encoding="UTF-8") as f:
        json.dump(deck_models_map, f, ensure_ascii=False, indent=4)

    return Ok()


@beartype
def get_tidy_payload(kinote: KiNote, paths: Dict[str, ExtantFile]) -> str:
    """Get the payload for the note (HTML-tidied if necessary)."""
    # Get tidied html if it exists.
    tidyfields = {}
    for fieldname, fieldtext in kinote.fields.items():
        fid = get_field_note_id(kinote.n.id, fieldname)
        if fid in paths:
            tidyfields[fieldname] = paths[fid].read_text()
        else:
            tidyfields[fieldname] = fieldtext

    # Construct note repr from tidyfields map.
    lines = kinote.get_header_lines()
    for fieldname, fieldtext in tidyfields.items():
        lines.append("### " + fieldname)
        lines.append(fieldtext)
        lines.append("")

    return "\n".join(lines)


# TODO: Refactor into a safe function.
@beartype
def git_subprocess_pull(remote: str, branch: str) -> int:
    """Pull remote into branch using a subprocess call."""
    p = subprocess.run(
        [
            "git",
            "pull",
            "-v",
            "--allow-unrelated-histories",
            "--strategy-option",
            "theirs",
            remote,
            branch,
        ],
        check=False,
        capture_output=True,
    )
    pull_stderr = p.stderr.decode()
    logger.debug(f"\n{pull_stderr}")
    logger.debug(f"Return code: {p.returncode}")
    if p.returncode != 0:
        raise ValueError(pull_stderr)
    return p.returncode


@beartype
def echo(string: str, silent: bool = False) -> None:
    """Call `click.secho()` with formatting."""
    if not silent:
        click.secho(string, bold=True)
        # logger.info(string)


# TODO: Refactor into a safe function.
@beartype
def slugify(value: str, allow_unicode: bool = False) -> str:
    """
    Taken from https://github.com/django/django/blob/master/django/utils/text.py
    Convert to ASCII if 'allow_unicode' is False. Convert spaces or repeated
    dashes to single dashes. Remove characters that aren't alphanumerics,
    underscores, or hyphens. Convert to lowercase. Also strip leading and
    trailing whitespace, dashes, and underscores.
    """
    if allow_unicode:
        value = unicodedata.normalize("NFKC", value)
    else:
        value = (
            unicodedata.normalize("NFKD", value)
            .encode("ascii", "ignore")
            .decode("ascii")
        )
    value = re.sub(r"[^\w\s-]", "", value.lower())
    return re.sub(r"[-\s]+", "-", value).strip("-_")


@safe
@beartype
def tidy_html_recursively(root: ExtantDir, silent: bool) -> Result[bool, Exception]:
    """Call html5-tidy on each file in ``root``, editing in-place."""
    # Spin up subprocesses for tidying field HTML in-place.
    batches: List[List[ExtantFile]] = list(get_batches(frglob(root, "*"), BATCH_SIZE))
    for batch in tqdm(batches, ncols=TQDM_NUM_COLS, disable=silent):

        # Fail silently here, so as to not bother user with tidy warnings.
        command = ["tidy", "-q", "-m", "-i", "-omit", "-utf8", "--tidy-mark", "no"]
        command += batch
        try:
            subprocess.run(command, check=False, capture_output=True)
        except Exception as err:
            return Err(err)
    return Ok()


@safe
@beartype
def convert_stage_kirepo(
    stage_kirepo: KiRepo, kirepo: KiRepo
) -> Result[KiRepo, Exception]:
    """
    Convert the staging repository into a format that is amenable to taking
    diffs across all files in all submodules.

    To do this, we first convert all submodules into ordinary subdirectories of
    the git repository. Then we replace the dot git directory of the staging
    repo with the .git directory of the repo in ``.ki/no_submodules_tree/``,
    which, as its name suggests, is a copy of the main repository with all its
    submodules converted into directories.

    This is done in order to preserve the history of
    ``.ki/no_submodules_tree/``. The staging repository can be thought of as
    the next commit to this repo.

    We return a reloaded version of the staging repository, re-read from disk.
    """
    unsubmodule_repo(stage_kirepo.repo)

    # Shutil rmtree the stage repo .git directory.
    stage_git_dir: Res[NoPath] = rmtree(git_dir(stage_kirepo.repo))
    stage_root: ExtantDir = stage_kirepo.root
    del stage_kirepo

    # Copy the .git folder from ``no_submodules_tree`` into the stage repo.
    stage_git_dir = copytree(git_dir(kirepo.no_modules_repo), stage_git_dir)
    stage_root: Res[ExtantDir] = fparent(stage_git_dir)

    # Reload stage kirepo.
    stage_kirepo: Res[KiRepo] = M_kirepo(stage_root)

    return stage_kirepo


@safe
@beartype
def commit_stage_repo(kirepo: KiRepo, head: KiRepoRef) -> Result[bool, Exception]:
    """
    Commit the staging repo with a descriptive commit message that contains the
    reference commit hash of the relevant commit of the main repository. In
    this case (since we're cloning), it should always be the first commit of
    the main repository.
    """
    kirepo.repo.git.add(all=True)
    kirepo.repo.index.commit(f"Pull changes from ref {head.sha}")
    return Ok()


@safe
@beartype
def get_target(
    cwd: ExtantDir, col_file: ExtantFile, directory: str
) -> Result[EmptyDir, Exception]:
    # Create default target directory.
    return fmkdempty(Path(directory) if directory != "" else cwd / col_file.stem)


@click.group()
@click.version_option()
@beartype
def ki() -> None:
    """
    The universal CLI entry point for `ki`.

    Takes no arguments, only has three subcommands (clone, pull, push).
    """
    return


@ki.command()
@click.argument("collection")
@click.argument("directory", required=False, default="")
def clone(collection: str, directory: str = "") -> Result[bool, Exception]:
    """
    Clone an Anki collection into a directory.

    Parameters
    ----------
    collection : str
        The path to a `.anki2` collection file.
    directory : str, default=""
        An optional path to a directory to clone the collection into.
        Note: we check that this directory does not yet exist.
    """
    echo("Cloning.")
    col_file: Res[ExtantFile] = M_xfile(Path(collection))

    cwd: ExtantDir = fcwd()
    targetdir: Res[EmptyDir] = get_target(cwd, col_file, directory)
    md5sum: Res[str] = _clone(col_file, targetdir, msg="Initial commit", silent=False)

    # Check that we are inside a ki repository, and get the associated collection.
    targetdir: Res[ExtantDir] = M_xdir(targetdir)
    kirepo: Res[KiRepo] = M_kirepo(targetdir)

    # Get reference to HEAD of current repo.
    head: Res[KiRepoRef] = M_head_kirepo_ref(kirepo)

    # Get staging repository in temp directory, and copy to ``no_submodules_tree``.

    # Copy current kirepo into a temp directory (the STAGE), hard reset to HEAD.
    stage_kirepo: Res[KiRepo] = get_ephemeral_kirepo(STAGE_SUFFIX, head, md5sum)
    stage_kirepo = convert_stage_kirepo(stage_kirepo, kirepo)
    processed: OkErr = postprocess_clone_repos(stage_kirepo, head)
    return cleanup_after_clone(targetdir, kirepo, head, processed)


@safe
@beartype
def postprocess_clone_repos(
    stage_kirepo: KiRepo, head: KiRepoRef
) -> Result[ExtantDir, Exception]:
    committed: OkErr = commit_stage_repo(stage_kirepo, head)
    if committed.is_err():
        return committed

    # Completely annihilate the ``.ki/no_submodules_tree``
    # directory/repository, and replace it with ``stage_kirepo``. This is a
    # sensible operation because earlier, we copied the ``.git/`` directory
    # from ``.ki/no_submodules_tree`` to the staging repo. So the history is
    # preserved.
    no_modules_root: NoPath = rmtree(working_dir(head.kirepo.no_modules_repo))
    copied: OkErr = copytree(stage_kirepo.root, no_modules_root)

    return copied


@beartype
def cleanup_after_clone(
    targetdir: OkErr, kirepo: OkErr, head: OkErr, processed: OkErr
) -> Result[bool, Exception]:
    """
    Cleanup without lifting errors.

    Unlike nearly every other function, we do not want to forward errors into
    the return value here, so it does not make sense for this function to be
    decorated with @safe.

    Write the HEAD commit hash of the ki repository we've just cloned to the
    file ``.ki/last_push``, which is a file that always exists if a ki
    repository is valid, and contains either nothing, or a single SHA
    referencing either the first commit (i.e. the commit made during a clone),
    or the commit of the last successful ``push()`` call.

    The parameters are in a very specific order, which is to say that they're
    in the order in which errors would be raised within ``clone()``. The
    control flow does not depend on the order of the arguments, but the
    if-return blocks below should mirror it, and having the parameters in the
    same order is nice.
    """
    # If any of the Result parameters are errors, we should print a 'failed' message.
    if targetdir.is_err() or kirepo.is_err() or head.is_err() or processed.is_err():
        echo("Failed: exiting.")

    # We get an error here only in the case where the ``M_xdir()`` call failed
    # on ``targetdir``. We cannot assume that it doesn't exist, because we may
    # have returned the exception inside ``fmkdempty()``, which errors-out when
    # the target already exists and is nonempty. This means we definitely do
    # not want to remove ``targetdir`` or its contents in this case, because we
    # would be deleting the user's data.
    if targetdir.is_err():
        return targetdir

    # Otherwise, we must have that either we created ``targetdir`` and it did
    # not exist prior, or it was an empty directory before. In either case, we
    # can probably remove it safely. We do this bit without using our @safe-d
    # wrappers because it is very important that these three lines do not fail.
    # If they do, we would have an 'exception raised while handling an
    # exception'-type error to display to the user, which would be gross. So we
    # assume that these statements can never raise exceptions.
    # TODO: Consider removing only its contents instead.
    targetdir: ExtantDir = targetdir.unwrap()
    if targetdir.is_dir():
        shutil.rmtree(targetdir)

    # We return the first ``Err`` value we find, so that the failure message
    # indicates the first thing that went wrong.
    if kirepo.is_err():
        return kirepo
    if head.is_err():
        return head
    if processed.is_err():
        return processed

    # Dump HEAD ref of current repo in ``.ki/last_push``.
    kirepo.last_push_file.write_text(head.unwrap().sha)

    return Ok()


@safe
@beartype
def _clone(
    col_file: ExtantFile, targetdir: EmptyDir, msg: str, silent: bool
) -> Result[str, Exception]:
    """
    Clone an Anki collection into a directory.

    The caller, realistically only ``clone()``, expects that ``targetdir`` will
    be the root of a valid ki repository after this function is called, so we
    need to do our repo initialization with gitpython in here, as opposed to in
    ``clone()``.

    Parameters
    ----------
    col_file : pathlib.Path
        The path to a `.anki2` collection file.
    targetdir : pathlib.Path
        A path to a directory to clone the collection into.
        Note: we check that this directory is empty.
    msg : str
        Message for initial commit.
    silent : bool
        Indicates whether we are calling `_clone()` from `pull()`.

    Returns
    -------
    git.Repo
        The cloned repository.
    """
    echo(f"Found .anki2 file at '{col_file}'", silent=silent)

    # Create .ki subdirectory.
    ki_dir: EmptyDir = ffmksubdir(targetdir, Path(KI))

    leaves: Res[Leaves] = fmkleaves(
        ki_dir,
        files={CONFIG_FILE: CONFIG_FILE, LAST_PUSH_FILE: LAST_PUSH_FILE},
        dirs={BACKUPS_DIR: BACKUPS_DIR, NO_SM_DIR: NO_SM_DIR},
    )

    md5sum = md5(col_file)
    echo(f"Computed md5sum: {md5sum}", silent)
    echo(f"Cloning into '{targetdir}'...", silent=silent)

    # Add `.ki/` to gitignore.
    ignore_path = targetdir / GITIGNORE_FILE
    ignore_path.write_text(".ki/\n")

    # Write notes to disk. We do explicit error checking here because if we
    # don't the repository initialization will run even when there's a failure.
    # This would be very bad for speed, because gitpython calls have quite a
    # bit of overhead sometimes (although maybe not for ``Repo.init()`` calls,
    # since they aren't networked).
    wrote: OkErr = write_repository(col_file, targetdir, leaves, silent)
    initialized: OkErr = init_repos(targetdir, leaves, msg, wrote)
    if initialized.is_err():
        return initialized

    # Store the md5sum of the anki collection file in the hashes file (we
    # always append, never overwrite).
    append_md5sum(ki_dir, col_file.name, md5sum, silent)

    return Ok(md5sum)


@safe
@beartype
def init_repos(
    targetdir: ExtantDir, leaves: Leaves, msg: str, wrote: bool
) -> Result[bool, Exception]:
    """
    Initialize both git repos and commit contents of the main one.
    """
    repo = git.Repo.init(targetdir, initial_branch=BRANCH_NAME)
    repo.git.add(all=True)
    _ = repo.index.commit(msg)

    # Initialize the copy of the repository with submodules replaced with
    # subdirectories that lives in ``.ki/no_submodules_tree/``.
    _ = git.Repo.init(leaves.files[NO_SM_DIR], initial_branch=BRANCH_NAME)

    return Ok()


@ki.command()
@beartype
def pull() -> Result[bool, Exception]:
    """
    Pull from a preconfigured remote Anki collection into an existing ki
    repository.
    """

    # Check that we are inside a ki repository, and get the associated collection.
    cwd: ExtantDir = fcwd()
    kirepo: Res[KiRepo] = M_kirepo(cwd)
    con: Res[sqlite3.Connection] = lock(kirepo)
    pulled: OkErr = _pull(kirepo, con)
    return pulled


@safe
@beartype
def _pull(kirepo: KiRepo, con: sqlite3.Connection) -> Result[bool, Exception]:
    """
    This function exists because we need to unfold the ``kirepo`` Result in ``pull()``.
    """
    md5sum: str = md5(kirepo.col_file)
    hashes: List[str] = kirepo.hashes_file.read_text().split("\n")
    hashes = list(filter(lambda l: l != "", hashes))
    logger.debug(f"Hashes:\n{pp.pformat(hashes)}")
    if md5sum in hashes[-1]:
        echo("ki pull: up to date.")
        return Ok(unlock(con))

    echo(f"Pulling from '{kirepo.col_file}'")
    echo(f"Computed md5sum: {md5sum}")

    # Git clone `repo` at commit SHA of last successful `push()`.
    sha: str = kirepo.last_push_file.read_text()
    ref: Res[RepoRef] = M_repo_ref(kirepo.repo, sha)
    last_push_repo: Res[git.Repo] = get_ephemeral_repo(LOCAL_SUFFIX, ref, md5sum)

    # Ki clone collection into an ephemeral ki repository at `anki_remote_root`.
    msg = f"Fetch changes from DB at '{kirepo.col_file}' with md5sum '{md5sum}'"
    anki_remote_root: EmptyDir = ffmksubdir(ffmkdtemp(), REMOTE_SUFFIX / md5sum)

    # This should return the repository as well.
    cloned: Res[str] = _clone(kirepo.col_file, anki_remote_root, msg, silent=True)
    pulled: OkErr = pull_changes_from_remote_repo(
        kirepo, anki_remote_root, last_push_repo, md5sum, cloned
    )
    if pulled.is_err():
        return pulled
    return Ok(unlock(con))


@safe
@beartype
def pull_theirs_from_remote(
    repo: git.Repo, root: ExtantDir, remote: git.Remote
) -> Result[bool, Exception]:
    cwd: ExtantDir = fchdir(root)
    echo(f"Pulling into {root}")
    repo.git.config("pull.rebase", "false")
    # TODO: This is not yet safe. Consider rewriting with gitpython.
    git_subprocess_pull(REMOTE_NAME, BRANCH_NAME)
    repo.delete_remote(remote)
    fchdir(cwd)
    return Ok()


@safe
@beartype
def pull_changes_from_remote_repo(
    kirepo: KiRepo,
    anki_remote_root: ExtantDir,
    last_push_repo: git.Repo,
    md5sum: str,
    _cloned: str,
) -> Result[bool, Exception]:
    """
    Load the git repository at ``anki_remote_root``, force pull (preferring
    'theirs', i.e. the new stuff from the sqlite3 database) changes from that
    repository (which is cloned straight from the collection, which in general
    may have new changes) into ``last_push_repo``, and then pull
    ``last_push_repo`` into the main repository.

    We pull in this sequence in order to avoid merge conflicts. Since we first
    pull into a snapshot of the repository as it looked when we last pushed to
    the database, we know that there cannot be any merge conflicts, because to
    git, it just looks like we haven't made any changes since then. Then we
    pull the result of that merge into our actual repository. So there could
    still be merge conflicts at that point, but they will only be 'genuine'
    merge conflicts in some sense, because as a result of using this snapshot
    strategy, we give the anki collection the appearance of being a persistent
    remote git repo. If we didn't do this, the fact that we did a fresh clone
    of the database every time would mean that everything would look like a
    merge conflict, because there is no shared history.
    """
    remote_repo: Res[git.Repo] = M_repo(anki_remote_root)

    # Create git remote pointing to anki remote repo.
    anki_remote: OkErr = fcreate_local_remote(last_push_repo, remote_repo, REMOTE_NAME)

    # Pull anki remote repo into ``last_push_repo``.
    last_push_root: Res[ExtantDir] = working_dir(last_push_repo)
    pulled: OkErr = pull_theirs_from_remote(last_push_repo, last_push_root, anki_remote)
    if pulled.is_err():
        return pulled

    # Create remote pointing to ``last_push`` repo and pull into ``repo``.
    last_push_remote = kirepo.repo.create_remote(REMOTE_NAME, last_push_repo.git_dir)
    kirepo.repo.git.config("pull.rebase", "false")
    p = subprocess.run(
        ["git", "pull", "-v", REMOTE_NAME, BRANCH_NAME],
        check=False,
        capture_output=True,
    )
    click.secho(f"{p.stdout.decode()}", bold=True)
    click.secho(f"{p.stderr.decode()}", bold=True)
    kirepo.repo.delete_remote(last_push_remote)

    # Append to hashes file.
    append_md5sum(kirepo.ki_dir, kirepo.col_file.name, md5sum, silent=True)

    # Check that md5sum hasn't changed.
    if md5(kirepo.col_file) != md5sum:
        return Err(CollectionChecksumError(kirepo.col_file))
    return Ok()


# PUSH


@ki.command()
@beartype
def push() -> Result[bool, Exception]:
    """Push a ki repository into a .anki2 file."""
    pp.install_extras(exclude=["ipython", "django", "ipython_repr_pretty"])

    # Check that we are inside a ki repository, and get the associated collection.
    cwd: ExtantDir = fcwd()
    kirepo: Res[KiRepo] = M_kirepo(cwd)
    con: Res[sqlite3.Connection] = lock(kirepo)
    return _push(kirepo, con)


@safe
@beartype
def _push(kirepo: KiRepo, con: sqlite3.Connection) -> Result[bool, Exception]:
    """ """
    md5sum: str = md5(kirepo.col_file)
    hashes: List[str] = kirepo.hashes_file.read_text().split("\n")
    hashes = list(filter(lambda l: l != "", hashes))
    logger.debug(f"Hashes:\n{pp.pformat(hashes)}")
    if md5sum not in hashes[-1]:
        return Err(UpdatesRejectedError(str(kirepo.col_file)))

    # Get reference to HEAD of current repo.
    head: Res[KiRepoRef] = M_head_kirepo_ref(kirepo)

    # Copy current kirepo into a temp directory (the STAGE), hard reset to HEAD.
    stage_kirepo: Res[KiRepo] = get_ephemeral_kirepo(STAGE_SUFFIX, head, md5sum)
    stage_kirepo = convert_stage_kirepo(stage_kirepo, kirepo)

    # This statement cannot be any farther down because we must get a reference
    # to HEAD *before* we commit, and then after the following line, the
    # reference we got will be HEAD~1, hence the variable name.
    head_1: Res[RepoRef] = M_head_repo_ref(stage_kirepo.repo)
    committed: OkErr = commit_stage_repo(stage_kirepo, head)
    if committed.is_err():
        return committed

    # Get filter function.
    ignore_fn = functools.partial(path_ignore_fn, patterns=IGNORE, root=kirepo.root)

    head_kirepo: Res[KiRepo] = get_ephemeral_kirepo(LOCAL_SUFFIX, head, md5sum)

    # Read grammar.
    # UNSAFE! Should we assume this always exists? A nice error message should
    # be printed on initialization if the grammar file is missing. No
    # computation should be done, and none of the click commands should work.
    grammar_path = Path(__file__).resolve().parent / "grammar.lark"
    grammar = grammar_path.read_text(encoding="UTF-8")

    # Instantiate parser.
    parser = Lark(grammar, start="file", parser="lalr")
    transformer = NoteTransformer()

    # Get deltas.
    deltas: Res[List[Delta]] = get_deltas_since_last_push(
        head_1, md5sum, ignore_fn, parser, transformer
    )
    new_models: Res[Dict[int, NotetypeDict]] = get_models_recursively(head_kirepo)

    return process_deltas(deltas, new_models, kirepo, md5sum, parser, transformer)


@safe
@beartype
def process_deltas(
    deltas: List[Delta],
    new_models: Dict[int, NotetypeDict],
    kirepo: KiRepo,
    md5sum: str,
    parser: Lark,
    transformer: NoteTransformer,
) -> Result[bool, Exception]:

    # If there are no changes, quit.
    if len(set(deltas)) == 0:
        echo("ki push: up to date.")
        return Ok()

    echo(f"Pushing to '{kirepo.col_file}'")
    echo(f"Computed md5sum: {md5sum}")
    echo(f"Verified md5sum matches latest hash in '{kirepo.hashes_file}'")

    # Copy collection to a temp directory.
    temp_col_dir: ExtantDir = ffmkdtemp()
    new_col_file = temp_col_dir / kirepo.col_file.name
    new_col_file: ExtantFile = ffcopyfile(kirepo.col_file, new_col_file, temp_col_dir)

    head: Res[RepoRef] = M_head_repo_ref(kirepo.repo)
    if head.is_err():
        echo("Failed: no commits in repository. Couldn't find HEAD ref.")
        return Ok()
    head = head.unwrap()
    echo(f"Generating local .anki2 file from latest commit: {head.sha}")
    echo(f"Writing changes to '{new_col_file}'...")

    # Edit the copy with `apy`.
    with Anki(path=new_col_file) as a:

        # Add all new models.
        for new_model in new_models.values():
            # NOTE: Mutates ``new_model`` (appends checksum to name).
            # UNSAFE: Potential KeyError!
            # This can be fixed by writing a strict type for NotetypeDict.
            if a.col.models.id_for_name(new_model["name"]) is None:
                a.col.models.ensure_name_unique(new_model)
                a.col.models.add(new_model)

        # Gather logging statements to display.
        log: List[str] = []
        msg = ""
        num_new_nids = 0

        # Stash both unstaged and staged files (including untracked).
        kirepo.repo.git.stash(include_untracked=True, keep_index=True)
        kirepo.repo.git.reset("HEAD", hard=True)

        is_delete = lambda d: d.status == GitChangeType.DELETED
        deletes: List[Delta] = list(filter(is_delete, deltas))
        logger.debug(f"Deleting {len(deletes)} notes.")

        # TODO: All this logic can be abstracted away from the process of
        # actually parsing notes and constructing Anki-specific objects. This
        # is just a series of filesystem ops. They should be put in a
        # standalone function and tested without anything related to Anki.
        for delta in tqdm(deltas, ncols=TQDM_NUM_COLS):

            if is_delete(delta):
                a.col.remove_notes(
                    [parse_markdown_note(parser, transformer, delta.path).nid]
                )
                a.modified = True
                continue

            # Get flatnote from parser, and add/edit/delete in collection.
            flatnote = parse_markdown_note(parser, transformer, delta.path)

            # If a kinote with this nid exists in DB, update it.
            # TODO: If relevant prefix of sort field has changed, we regenerate
            # the file. Recall that the sort field is used to determine the
            # filename. If the content of the sort field has changed, then we
            # may need to update the filename.
            try:
                kinote: KiNote = KiNote(a, a.col.get_note(flatnote.nid))
                update_kinote(kinote, flatnote)

            # Otherwise, we generate/reassign an nid for it.
            except anki.errors.NotFoundError:
                kinote: Optional[KiNote] = add_note_from_flatnote(a, flatnote)

                if kinote is not None:
                    log.append(f"Reassigned nid: '{flatnote.nid}' -> '{kinote.n.id}'")

                    # Get paths to note in local repo, as distinct from staging repo.
                    repo_note_path: Path = kirepo.root / delta.relpath

                    # If this is not an entirely new file, remove it.
                    if repo_note_path.is_file():
                        repo_note_path.unlink()

                    # Construct markdown file contents and write.
                    # UNSAFE: Can't pass None to 'get_sort_field_text()'. Need
                    # a ``MaybeNotetype``.
                    model: Optional[NotetypeDict] = kinote.n.note_type()
                    if model is None:
                        logger.warning(f"Couldn't find notetype for {kinote}")
                        continue
                    sort_field_text: Optional[str] = get_sort_field_text(
                        a, kinote.n, model
                    )
                    if sort_field_text is None:
                        logger.warning(f"Couldn't find sort field for {kinote}")
                        continue
                    parent: ExtantDir = fforce_mkdir(repo_note_path.parent)
                    new_note_path: ExtantFile = get_note_path(sort_field_text, parent)
                    new_note_path.write_text(str(kinote), encoding="UTF-8")

                    new_note_relpath = os.path.relpath(new_note_path, kirepo.root)
                    num_new_nids += 1
                    msg += f"Wrote note '{kinote.n.id}' in file {new_note_relpath}\n"

        # Commit nid reassignments.
        logger.warning(f"Reassigned {num_new_nids} nids.")
        if num_new_nids > 0:
            msg = "Generated new nid(s).\n\n" + msg

            # Commit in all submodules (doesn't support recursing yet).
            for sm in kirepo.repo.submodules:
                subrepo: git.Repo = sm.update().module()
                subrepo.git.add(all=True)
                subrepo.index.commit(msg)

            # Commit in main repository.
            kirepo.repo.git.add(all=True)
            _ = kirepo.repo.index.commit(msg)

    # Backup collection file and overwrite collection.
    backup(kirepo.col_file)
    new_col_file = ffcopyfile(new_col_file, kirepo.col_file, fparent(kirepo.col_file))
    echo(f"Overwrote '{kirepo.col_file}'")

    # Append to hashes file.
    new_md5sum = md5(new_col_file)
    append_md5sum(kirepo.ki_dir, new_col_file.name, new_md5sum, silent=False)

    # Completely annihilate the ``.ki/no_submodules_tree``
    # directory/repository, and replace it with ``stage_kirepo``. This is a
    # sensible operation because earlier, we copied the ``.git/`` directory
    # from ``.ki/no_submodules_tree`` to the staging repo. So the history is
    # preserved.
    no_modules_root: NoPath = rmtree(working_dir(kirepo.no_modules_repo))
    copytree(stage_kirepo.root, no_modules_root)

    # Dump HEAD ref of current repo in ``.ki/last_push``.
    kirepo.last_push_file.write_text(head.sha)

    # Unlock Anki SQLite DB.
    unlock(con)

    return Ok()
