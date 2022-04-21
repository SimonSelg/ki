#!/usr/bin/env python3
"""Types for ki."""
import textwrap
from enum import Enum
from pathlib import Path
from dataclasses import dataclass

import git
import prettyprinter as pp
from anki.collection import Note

from beartype import beartype
from beartype.typing import List, Dict, Any, Optional

from ki.transformer import FlatNote

MODELS_FILE = "models.json"
HINT = (
    "hint: Updates were rejected because the tip of your current branch is behind\n"
    + "hint: the Anki remote collection. Integrate the remote changes (e.g.\n"
    + "hint: 'ki pull ...') before pushing again."
)


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
class Field:
    """A typechecked version of `anki.models.FieldDict` for use within ki."""

    name: str
    ord: Optional[int]


@beartype
@dataclass(frozen=True)
class Template:
    """A typechecked version of `anki.models.TemplateDict` for use within ki."""

    name: str
    qfmt: str
    afmt: str
    ord: Optional[int]


@beartype
@dataclass(frozen=True)
class Notetype:
    """A typechecked version of `anki.models.NotetypeDict` for use within ki."""

    id: int
    name: str
    type: int
    flds: List[Field]
    tmpls: List[Template]
    sortf: Field

    # A copy of the `NotetypeDict` object as it was returned from the Anki
    # database. We keep this around to preserve extra keys that may not always
    # exist, but the ones above should be required for Anki to function.
    dict: Dict[str, Any]


@beartype
@dataclass(frozen=True)
class ColNote:
    """A note that exists in the Anki DB."""

    n: Note
    new: bool
    deck: str
    title: str
    old_nid: int
    markdown: bool
    notetype: Notetype
    sortf_text: str


@beartype
@dataclass(frozen=True)
class KiRepoRef:
    """
    UNSAFE: A repo-commit pair, where `sha` is guaranteed to be an extant
    commit hash of `repo`.
    """

    kirepo: KiRepo
    sha: str


@beartype
@dataclass(frozen=True)
class RepoRef:
    """
    UNSAFE: A repo-commit pair, where `sha` is guaranteed to be an extant
    commit hash of `repo`.
    """

    repo: git.Repo
    sha: str


@beartype
@dataclass(frozen=True)
class Leaves:
    root: ExtantDir
    files: Dict[str, ExtantFile]
    dirs: Dict[str, EmptyDir]


# EXCEPTIONS


class MissingFileError(FileNotFoundError):
    @beartype
    def __init__(self, path: Path, info: str = ""):
        msg = f"File not found: '{path}'{info.rstrip()}"
        super().__init__(textwrap.fill(textwrap.dedent(msg), width=80))


class MissingDirectoryError(Exception):
    @beartype
    def __init__(self, path: Path, info: str = ""):
        msg = f"Directory not found: '{path}'{info.rstrip()}"
        super().__init__(textwrap.fill(textwrap.dedent(msg), width=80))


class ExpectedFileButGotDirectoryError(FileNotFoundError):
    @beartype
    def __init__(self, path: Path, info: str = ""):
        msg = "A file was expected at this location, but got a directory: "
        msg += f"'{path}'{info.rstrip()}"
        super().__init__(textwrap.fill(textwrap.dedent(msg), width=80))


class ExpectedDirectoryButGotFileError(Exception):
    @beartype
    def __init__(self, path: Path, info: str = ""):
        msg = "A directory was expected at this location, but got a file: "
        msg += f"'{path}'{info.rstrip()}"
        super().__init__(textwrap.fill(textwrap.dedent(msg), width=80))


class ExpectedEmptyDirectoryButGotNonEmptyDirectoryError(Exception):
    @beartype
    def __init__(self, path: Path, info: str = ""):
        msg = "An empty directory was expected at this location, but it is nonempty: "
        msg += f"'{path}'{info.rstrip()}"
        super().__init__(textwrap.fill(textwrap.dedent(msg), width=80))


class StrangeExtantPathError(Exception):
    @beartype
    def __init__(self, path: Path, info: str = ""):
        msg = "A normal file or directory was expected, but got a weird pseudofile "
        msg += "(e.g. a socket, or a device): "
        msg += f"'{path}'{info.rstrip()}"
        super().__init__(textwrap.fill(textwrap.dedent(msg), width=80))


class NotKiRepoError(Exception):
    @beartype
    def __init__(self):
        msg = "fatal: not a ki repository (or any parent up to mount point /)\n"
        msg += "Stopping at filesystem boundary."
        super().__init__(textwrap.fill(textwrap.dedent(msg), width=80))


class UpdatesRejectedError(Exception):
    @beartype
    def __init__(self, col_file: ExtantFile):
        msg = f"Failed to push some refs to '{col_file}'\n{HINT}"
        super().__init__(textwrap.fill(textwrap.dedent(msg), width=80))


class TargetExistsError(Exception):
    @beartype
    def __init__(self, target: Path):
        msg = f"fatal: destination path '{target}' already exists and is "
        msg += "not an empty directory."
        super().__init__(textwrap.fill(textwrap.dedent(msg), width=80))


class GitRefNotFoundError(Exception):
    @beartype
    def __init__(self, repo: git.Repo, sha: str):
        msg = f"Repo at '{repo.working_dir}' doesn't contain ref '{sha}'"
        super().__init__(textwrap.fill(textwrap.dedent(msg), width=80))


class CollectionChecksumError(Exception):
    @beartype
    def __init__(self, col_file: ExtantFile):
        msg = f"Checksum mismatch on {col_file}. Was file changed?"
        super().__init__(textwrap.fill(textwrap.dedent(msg), width=80))


class MissingNotetypeError(Exception):
    @beartype
    def __init__(self, model: str):
        msg = f"""
        Notetype '{model}' doesn't exist. Create it in Anki before adding notes
        via ki. This may be caused by a corrupted '{MODELS_FILE}' file. The
        models file must contain definitions for all models that appear in all
        note files.
        """
        super().__init__(textwrap.fill(textwrap.dedent(msg), width=80))


class MissingFieldOrdinalError(Exception):
    @beartype
    def __init__(self, ord: int, nt: Dict[str, Any]):
        msg = f"Field with ordinal {ord} missing from notetype '{pp.pformat(nt)}'."
        super().__init__(textwrap.fill(textwrap.dedent(msg), width=80))


class MissingNoteIdError(Exception):
    @beartype
    def __init__(self, nid: int):
        msg = f"Failed to locate note with nid '{nid}' in Anki database."
        super().__init__(textwrap.fill(textwrap.dedent(msg), width=80))


class NotetypeMismatchError(Exception):
    @beartype
    def __init__(self, flatnote: FlatNote, new_notetype: Notetype):
        msg = f"Notetype '{flatnote.model}' "
        msg += f"specified in FlatNote with nid '{flatnote.nid}' "
        msg += f"does not match passed notetype '{new_notetype}'. "
        msg += "This should NEVER happen, "
        msg += "and indicates a bug in the caller to 'update_note()'."
        super().__init__(textwrap.fill(textwrap.dedent(msg), width=80))


# WARNINGS


# TODO: Make this warning more descriptive. Should given the note id, the path,
# the field(s) which are missing, and the model.
class NoteFieldValidationWarning(Warning):
    pass


class UnhealthyNoteWarning(Warning):
    pass
