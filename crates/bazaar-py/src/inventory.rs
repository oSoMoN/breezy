use bazaar::inventory::{describe_change, detect_changes, Entry};
use bazaar::inventory_delta::{
    InventoryDeltaEntry, InventoryDeltaInconsistency, InventoryDeltaParseError,
    InventoryDeltaSerializeError,
};
use bazaar::{FileId, RevisionId};
use breezy_osutils::Kind;
use pyo3::class::basic::CompareOp;
use pyo3::exceptions::{PyIndexError, PyKeyError, PyNotImplementedError};
use pyo3::prelude::*;
use pyo3::pyclass_init::PyClassInitializer;
use pyo3::types::{PyBytes, PyDict};
use pyo3::wrap_pyfunction;
use pyo3::{create_exception, import_exception};
use std::collections::HashMap;

import_exception!(breezy.bzr.inventory, InvalidEntryName);
import_exception!(breezy.errors, NoSuchId);
import_exception!(breezy.errors, BzrCheckError);
import_exception!(breezy.errors, InvalidNormalization);
import_exception!(breezy.errors, InconsistentDelta);
import_exception!(breezy.errors, BzrError);
create_exception!(breezy.inventory_delta, IncompatibleInventoryDelta, BzrError);
create_exception!(breezy.inventory_delta, InventoryDeltaError, BzrError);

fn kind_from_str(kind: &str) -> Option<Kind> {
    match kind {
        "file" => Some(Kind::File),
        "directory" => Some(Kind::Directory),
        "tree-reference" => Some(Kind::TreeReference),
        "symlink" => Some(Kind::Symlink),
        _ => None,
    }
}

fn check_name(name: &str) -> PyResult<()> {
    if !is_valid_name(name) {
        Err(InvalidEntryName::new_err((name.to_string(),)))
    } else {
        Ok(())
    }
}

fn common_ie_check(
    slf: PyObject,
    ie: &Entry,
    py: Python,
    checker: &PyObject,
    rev_id: &[u8],
    inv: PyObject,
) -> PyResult<()> {
    if let Some(parent_id) = ie.parent_id() {
        let present = inv
            .call_method1(
                py,
                "has_id",
                (PyBytes::new(py, parent_id.bytes()).to_object(py),),
            )?
            .extract::<bool>(py)?;
        if !present {
            return Err(BzrCheckError::new_err(format!(
                "missing parent {{{}}} in inventory for revision {{{}}}",
                parent_id,
                String::from_utf8(rev_id.to_vec()).unwrap()
            )));
        }
    }

    checker.call_method1(py, "_add_entry_to_text_key_references", (inv, slf))?;

    Ok(())
}

#[pyclass(subclass)]
pub struct InventoryEntry(Entry);

#[pymethods]
impl InventoryEntry {
    fn has_text(&self) -> bool {
        matches!(&self.0, Entry::File { .. })
    }

    fn kind_character(&self) -> &'static str {
        self.0.kind().marker()
    }

    #[getter]
    fn kind(&self) -> &'static str {
        self.0.kind().to_string()
    }

    #[getter]
    fn get_name(&self) -> &str {
        match &self.0 {
            Entry::File { name, .. } => name,
            Entry::Directory { name, .. } => name,
            Entry::TreeReference { name, .. } => name,
            Entry::Link { name, .. } => name,
        }
    }

    #[setter]
    fn set_name(&mut self, name: String) {
        match &mut self.0 {
            Entry::File { name: n, .. } => *n = name,
            Entry::Directory { name: n, .. } => *n = name,
            Entry::TreeReference { name: n, .. } => *n = name,
            Entry::Link { name: n, .. } => *n = name,
        }
    }

    #[getter]
    fn get_file_id(&self, py: Python) -> PyObject {
        let file_id = &self.0.file_id();

        PyBytes::new(py, file_id.bytes()).into()
    }

    #[setter]
    fn set__file_id(&mut self, py: Python, file_id: &[u8]) {
        match &mut self.0 {
            Entry::File { file_id: f, .. } => *f = FileId::from(file_id),
            Entry::Directory { file_id: f, .. } => *f = FileId::from(file_id),
            Entry::TreeReference { file_id: f, .. } => *f = FileId::from(file_id),
            Entry::Link { file_id: f, .. } => *f = FileId::from(file_id),
        }
    }

    #[getter]
    fn get_parent_id(&self, py: Python) -> Option<PyObject> {
        let parent_id = &self.0.parent_id();

        parent_id
            .as_ref()
            .map(|parent_id| PyBytes::new(py, parent_id.bytes()).into())
    }

    #[setter]
    fn set_parent_id(&mut self, parent_id: Option<Vec<u8>>) {
        match &mut self.0 {
            Entry::File { parent_id: p, .. } => *p = parent_id.map(FileId::from),
            Entry::Directory { parent_id: p, .. } => *p = parent_id.map(FileId::from),
            Entry::TreeReference { parent_id: p, .. } => *p = parent_id.map(FileId::from),
            Entry::Link { parent_id: p, .. } => *p = parent_id.map(FileId::from),
        }
    }

    #[getter]
    fn get_revision(&self, py: Python) -> Option<PyObject> {
        let revision = &self.0.revision();

        revision
            .as_ref()
            .map(|revision| PyBytes::new(py, revision.bytes()).into())
    }

    #[setter]
    fn set_revision(&mut self, revision: Option<Vec<u8>>) {
        match &mut self.0 {
            Entry::File { revision: r, .. } => *r = revision.map(RevisionId::from),
            Entry::Directory { revision: r, .. } => *r = revision.map(RevisionId::from),
            Entry::TreeReference { revision: r, .. } => *r = revision.map(RevisionId::from),
            Entry::Link { revision: r, .. } => *r = revision.map(RevisionId::from),
        }
    }

    #[staticmethod]
    fn versionable_kind(kind: &str) -> bool {
        if let Some(kind) = kind_from_str(kind) {
            bazaar::inventory::versionable_kind(kind)
        } else {
            false
        }
    }

    #[getter]
    fn get_executable(&self) -> bool {
        match &self.0 {
            Entry::File { executable, .. } => *executable,
            _ => false,
        }
    }

    fn is_unmodified(&self, other: &InventoryEntry) -> bool {
        self.0.is_unmodified(&other.0)
    }

    fn detect_changes(&self, other: &InventoryEntry) -> (bool, bool) {
        detect_changes(&self.0, &other.0)
    }

    #[staticmethod]
    fn describe_change(slf: Option<&InventoryEntry>, other: Option<&InventoryEntry>) -> String {
        describe_change(slf.map(|s| &s.0), other.map(|o| &o.0)).to_string()
    }

    fn __richcmp__(&self, other: &InventoryEntry, op: CompareOp) -> PyResult<bool> {
        match op {
            CompareOp::Eq => Ok(self.0 == other.0),
            CompareOp::Ne => Ok(self.0 != other.0),
            _ => Err(PyNotImplementedError::new_err("")),
        }
    }

    fn _unchanged(&self, other: &InventoryEntry) -> bool {
        self.0.unchanged(&other.0)
    }

    /// Find possible per-file graph parents.
    ///
    /// This is currently defined by:
    /// Select the last changed revision in the parent inventory.
    /// Do deal with a short lived bug in bzr 0.8's development two entries
    /// that have the same last changed but different 'x' bit settings are
    /// changed in-place.
    fn parent_candidates(
        &self,
        py: Python,
        previous_inventories: Vec<PyObject>,
    ) -> PyResult<PyObject> {
        // revision:ie mapping for each ie found in previous_inventories
        let mut candidates: HashMap<&RevisionId, PyObject> = HashMap::new();
        // identify candidate head revision ids
        for inv in previous_inventories {
            match inv.call_method1(py, "get_entry", (self.get_file_id(py),)) {
                Ok(py_entry) => {
                    if let Ok(mut entry) = py_entry.extract::<PyRefMut<InventoryEntry>>(py) {
                        if let Some(revision) = entry.0.revision() {
                            if let Some(candidate) = candidates.get_mut(revision) {
                                // same revision value in two different inventories:
                                // correct possible inconsistencies:
                                //  * there was a bug in revision updates with executable bit support
                                let mut candidate =
                                    candidate.extract::<PyRefMut<InventoryEntry>>(py)?;
                                match (&mut candidate.0, &mut entry.0) {
                                    (
                                        Entry::File {
                                            executable: candidate_executable,
                                            ..
                                        },
                                        Entry::File {
                                            executable: entry_executable,
                                            ..
                                        },
                                    ) => {
                                        if candidate_executable != entry_executable {
                                            *entry_executable = false;
                                            *candidate_executable = false;
                                        }
                                    }
                                    _ => {}
                                }
                            } else {
                                // add this revision as a candidate.
                                //candidates.insert(revision, py_entry);
                            }
                        }
                    }
                }
                Err(e) if e.is_instance_of::<NoSuchId>(py) => {}
                Err(e) => {
                    return Err(e);
                }
            }
        }
        let ret = PyDict::new(py);
        for (revision, entry) in candidates.iter() {
            ret.set_item(PyBytes::new(py, &revision.bytes()), entry)?;
        }
        Ok(ret.into_py(py))
    }
}

#[pyclass(subclass,extends=InventoryEntry)]
struct InventoryFile();

#[pymethods]
impl InventoryFile {
    #[new]
    fn new(
        file_id: Vec<u8>,
        name: String,
        parent_id: Option<Vec<u8>>,
    ) -> PyResult<(Self, InventoryEntry)> {
        check_name(name.as_str())?;
        let entry = Entry::File {
            file_id: FileId::from(file_id),
            name,
            parent_id: parent_id.map(FileId::from),
            revision: None,
            text_sha1: None,
            text_size: None,
            text_id: None,
            executable: false,
        };
        Ok((Self(), InventoryEntry(entry)))
    }

    #[setter]
    fn set_executable(slf: PyRefMut<Self>, executable: bool) {
        let mut s = slf.into_super();
        match &mut s.0 {
            Entry::File { executable: e, .. } => *e = executable,
            _ => panic!("Not a file"),
        }
    }

    #[getter]
    fn get_executable(slf: PyRef<Self>) -> bool {
        match slf.into_super().0 {
            Entry::File { executable, .. } => executable,
            _ => false,
        }
    }

    #[getter]
    fn get_text_sha1(slf: PyRef<Self>, py: Python) -> Option<PyObject> {
        let s = slf.into_super();
        match &s.0 {
            Entry::File { text_sha1, .. } => text_sha1
                .as_ref()
                .map(|text_sha1| PyBytes::new(py, text_sha1.as_ref()).into()),
            _ => panic!("Not a file"),
        }
    }

    #[setter]
    fn set_text_sha1(slf: PyRefMut<Self>, text_sha1: Option<Vec<u8>>) {
        let mut s = slf.into_super();
        match &mut s.0 {
            Entry::File { text_sha1: t, .. } => *t = text_sha1,
            _ => panic!("Not a file"),
        }
    }

    #[getter]
    fn get_text_size(slf: PyRef<Self>) -> Option<u64> {
        let s = slf.into_super();
        match &s.0 {
            Entry::File { text_size, .. } => *text_size,
            _ => panic!("Not a file"),
        }
    }

    #[setter]
    fn set_text_size(slf: PyRefMut<Self>, text_size: Option<u64>) {
        let mut s = slf.into_super();
        match &mut s.0 {
            Entry::File { text_size: t, .. } => *t = text_size,
            _ => panic!("Not a file"),
        }
    }

    #[getter]
    fn get_text_id(slf: PyRef<Self>, py: Python) -> Option<PyObject> {
        let s = slf.into_super();
        match &s.0 {
            Entry::File { text_id, .. } => text_id
                .as_ref()
                .map(|text_id| PyBytes::new(py, text_id).into()),
            _ => panic!("Not a file"),
        }
    }

    #[setter]
    fn set_text_id(slf: PyRefMut<Self>, text_id: Option<Vec<u8>>) {
        let mut s = slf.into_super();
        match &mut s.0 {
            Entry::File { text_id: t, .. } => *t = text_id,
            _ => panic!("Not a file"),
        }
    }

    fn copy(slf: PyRef<Self>, py: Python) -> PyResult<PyObject> {
        let s = slf.into_super();
        let init = PyClassInitializer::from(InventoryEntry(s.0.clone()));
        let init = init.add_subclass(Self());
        Ok(PyCell::new(py, init)?.to_object(py))
    }

    fn __repr__(slf: PyRef<Self>, py: Python) -> PyResult<String> {
        let s = slf.into_super();
        Ok(match &s.0 {
            Entry::File {
                name,
                file_id,
                parent_id,
                text_sha1,
                text_size,
                revision,
                ..
            } => format!(
                "InventoryFile({}, {}, parent_id={}, sha1={}, len={}, revision={})",
                PyBytes::new(py, file_id.bytes())
                    .to_object(py)
                    .as_ref(py)
                    .repr()?,
                name.to_object(py).as_ref(py).repr()?,
                parent_id
                    .as_ref()
                    .map(|p| PyBytes::new(py, p.bytes()))
                    .to_object(py)
                    .as_ref(py)
                    .repr()?,
                text_sha1
                    .as_ref()
                    .map(|r| PyBytes::new(py, r).to_object(py))
                    .to_object(py)
                    .as_ref(py)
                    .repr()?,
                text_size.to_object(py).as_ref(py).repr()?,
                revision
                    .as_ref()
                    .map(|r| PyBytes::new(py, r.bytes()).to_object(py))
                    .to_object(py)
                    .as_ref(py)
                    .repr()?,
            ),
            _ => panic!("Not a file"),
        })
    }

    fn check(
        slf: &PyCell<Self>,
        py: Python,
        checker: PyObject,
        rev_id: Vec<u8>,
        inv: PyObject,
    ) -> PyResult<()> {
        let spr = slf.borrow().into_super();
        common_ie_check(
            slf.to_object(py),
            &spr.0,
            py,
            &checker,
            rev_id.as_slice(),
            inv,
        )?;

        let (file_id, revision, text_sha1, text_size) = match spr.0 {
            Entry::File {
                ref text_sha1,
                ref file_id,
                ref revision,
                text_size,
                ..
            } => (file_id, revision, text_sha1, text_size),
            _ => panic!("Not a file"),
        };

        checker.call_method1(
            py,
            "add_pending_item",
            (
                PyBytes::new(py, rev_id.as_slice()).to_object(py),
                (
                    "texts",
                    PyBytes::new(py, file_id.bytes()).to_object(py),
                    revision
                        .as_ref()
                        .map(|p| PyBytes::new(py, p.bytes()).to_object(py)),
                ),
                PyBytes::new(py, b"text").to_object(py),
                text_sha1
                    .as_ref()
                    .map(|t| PyBytes::new(py, t.as_slice()).to_object(py)),
            ),
        )?;

        if text_size.is_none() {
            checker.getattr(py, "_report_items")?.call_method1(
                py,
                "append",
                (format!(
                    "fileid {{{}}} in {{{}}} has None for text_size",
                    String::from_utf8(file_id.bytes().to_vec()).unwrap(),
                    String::from_utf8(rev_id).unwrap()
                ),),
            )?;
        }

        Ok(())
    }
}

#[pyclass(subclass,extends=InventoryEntry)]
struct InventoryDirectory();

#[pymethods]
impl InventoryDirectory {
    #[new]
    fn new(
        file_id: Vec<u8>,
        name: String,
        parent_id: Option<Vec<u8>>,
    ) -> PyResult<(Self, InventoryEntry)> {
        check_name(name.as_str())?;
        let entry = Entry::Directory {
            file_id: FileId::from(file_id),
            name,
            parent_id: parent_id.map(FileId::from),
            revision: None,
        };
        Ok((Self(), InventoryEntry(entry)))
    }

    fn copy(slf: PyRef<Self>, py: Python) -> PyResult<PyObject> {
        let s = slf.into_super();
        let init = PyClassInitializer::from(InventoryEntry(s.0.clone()));
        let init = init.add_subclass(Self());
        Ok(PyCell::new(py, init)?.to_object(py))
    }

    #[getter]
    fn get_text_size(&self, py: Python) -> PyObject {
        py.None()
    }

    #[getter]
    fn get_text_sha1(&self, py: Python) -> PyObject {
        py.None()
    }

    fn __repr__(slf: PyRef<Self>, py: Python) -> PyResult<String> {
        let s = slf.into_super();
        Ok(match &s.0 {
            Entry::Directory {
                name,
                file_id,
                parent_id,
                revision,
                ..
            } => format!(
                "InventoryDirectory({}, {}, parent_id={}, revision={})",
                PyBytes::new(py, file_id.bytes())
                    .to_object(py)
                    .as_ref(py)
                    .repr()?,
                name.to_object(py).as_ref(py).repr()?,
                parent_id
                    .as_ref()
                    .map(|p| PyBytes::new(py, p.bytes()))
                    .to_object(py)
                    .as_ref(py)
                    .repr()?,
                revision
                    .as_ref()
                    .map(|r| PyBytes::new(py, r.bytes()))
                    .to_object(py)
                    .as_ref(py)
                    .repr()?,
            ),
            _ => panic!("Not a directory"),
        })
    }

    fn check(
        slf: &PyCell<Self>,
        py: Python,
        checker: PyObject,
        rev_id: Vec<u8>,
        inv: PyObject,
    ) -> PyResult<()> {
        let spr = slf.borrow().into_super();
        common_ie_check(
            slf.to_object(py),
            &spr.0,
            py,
            &checker,
            rev_id.as_slice(),
            inv,
        )?;

        // In non rich root repositories we do not expect a file graph for the
        // root.
        if spr.0.name().is_empty() && !checker.getattr(py, "rich_roots")?.extract::<bool>(py)? {
            return Ok(());
        }
        // Directories are stored as an empty file, but the file should exist
        // to provide a per-fileid log. The hash of every directory content is
        // "da..." below (the sha1sum of '').
        checker.call_method1(
            py,
            "add_pending_item",
            (
                PyBytes::new(py, rev_id.as_slice()).to_object(py),
                (
                    "texts",
                    PyBytes::new(py, spr.0.file_id().bytes()).to_object(py),
                    spr.0
                        .revision()
                        .map(|p| PyBytes::new(py, p.bytes()).to_object(py)),
                ),
                PyBytes::new(py, b"text").to_object(py),
                PyBytes::new(py, b"da39a3ee5e6b4b0d3255bfef95601890afd80709").to_object(py),
            ),
        )?;

        Ok(())
    }
}

#[pyclass(subclass,extends=InventoryEntry)]
struct TreeReference();

#[pymethods]
impl TreeReference {
    #[new]
    fn new(
        file_id: Vec<u8>,
        name: String,
        parent_id: Option<Vec<u8>>,
        revision: Option<Vec<u8>>,
        reference_revision: Option<Vec<u8>>,
    ) -> PyResult<(Self, InventoryEntry)> {
        check_name(name.as_str())?;
        let entry = Entry::TreeReference {
            file_id: FileId::from(file_id),
            name,
            parent_id: parent_id.map(FileId::from),
            revision: revision.map(RevisionId::from),
            reference_revision: reference_revision.map(RevisionId::from),
        };
        Ok((Self(), InventoryEntry(entry)))
    }

    #[getter]
    fn get_reference_revision(slf: PyRef<Self>, py: Python) -> Option<PyObject> {
        let s = slf.into_super();
        match &s.0 {
            Entry::TreeReference {
                reference_revision, ..
            } => reference_revision
                .as_ref()
                .map(|reference_revision| PyBytes::new(py, reference_revision.bytes()).into()),
            _ => panic!("Not a tree reference"),
        }
    }

    #[setter]
    fn set_reference_revision(slf: PyRefMut<Self>, reference_revision: Option<Vec<u8>>) {
        let mut s = slf.into_super();
        match &mut s.0 {
            Entry::TreeReference {
                reference_revision: r,
                ..
            } => *r = reference_revision.map(RevisionId::from),
            _ => panic!("Not a tree reference"),
        }
    }

    fn copy(slf: PyRef<Self>, py: Python) -> PyResult<PyObject> {
        let s = slf.into_super();
        let init = PyClassInitializer::from(InventoryEntry(s.0.clone()));
        let init = init.add_subclass(Self());
        Ok(PyCell::new(py, init)?.to_object(py))
    }
}

#[pyclass(subclass,extends=InventoryEntry)]
struct InventoryLink();

#[pymethods]
impl InventoryLink {
    #[new]
    fn new(
        file_id: Vec<u8>,
        name: String,
        parent_id: Option<Vec<u8>>,
    ) -> PyResult<(Self, InventoryEntry)> {
        check_name(name.as_str())?;
        let entry = Entry::Link {
            file_id: FileId::from(file_id),
            name,
            parent_id: parent_id.map(FileId::from),
            symlink_target: None,
            revision: None,
        };
        Ok((Self(), InventoryEntry(entry)))
    }

    #[getter]
    fn get_symlink_target(slf: PyRef<Self>) -> Option<String> {
        let s = slf.into_super();
        match s.0 {
            Entry::Link {
                ref symlink_target, ..
            } => symlink_target.clone(),
            _ => panic!("Not a link"),
        }
    }

    #[setter]
    fn set_symlink_target(slf: PyRefMut<Self>, target: Option<String>) {
        match slf.into_super().0 {
            Entry::Link {
                ref mut symlink_target,
                ..
            } => *symlink_target = target,
            _ => panic!("Not a link"),
        }
    }

    fn copy(slf: PyRef<Self>, py: Python) -> PyResult<PyObject> {
        let s = slf.into_super();
        let init = PyClassInitializer::from(InventoryEntry(s.0.clone()));
        let init = init.add_subclass(Self());
        Ok(PyCell::new(py, init)?.to_object(py))
    }

    #[getter]
    fn get_text_size(&self, py: Python) -> PyObject {
        py.None()
    }

    #[getter]
    fn get_text_sha1(&self, py: Python) -> PyObject {
        py.None()
    }

    fn check(
        slf: &PyCell<Self>,
        py: Python,
        checker: PyObject,
        rev_id: Vec<u8>,
        inv: PyObject,
    ) -> PyResult<()> {
        let spr = slf.borrow().into_super();
        common_ie_check(
            slf.to_object(py),
            &spr.0,
            py,
            &checker,
            rev_id.as_slice(),
            inv,
        )?;

        if spr.0.symlink_target().is_none() {
            let report_items = checker.getattr(py, "_report_items")?;
            report_items.call_method1(
                py,
                "append",
                (format!(
                    "symlink {} has no target in revision {}",
                    String::from_utf8(spr.0.file_id().bytes().to_vec()).unwrap(),
                    spr.0
                        .revision()
                        .map(|p| String::from_utf8(p.bytes().to_vec()).unwrap())
                        .unwrap_or_else(|| String::from("None"))
                ),),
            )?;
        }

        // Symlinks are stored as ''
        checker.call_method1(
            py,
            "add_pending_item",
            (
                PyBytes::new(py, rev_id.as_slice()).to_object(py),
                (
                    "texts",
                    PyBytes::new(py, spr.0.file_id().bytes()),
                    spr.0.revision().map(|r| PyBytes::new(py, r.bytes())),
                ),
                PyBytes::new(py, b"text").to_object(py),
                PyBytes::new(py, b"da39a3ee5e6b4b0d3255bfef95601890afd80709").to_object(py),
            ),
        )?;
        Ok(())
    }
}

fn entry_to_py(py: Python, e: Entry) -> PyResult<PyObject> {
    let kind = e.kind();
    let init = PyClassInitializer::from(InventoryEntry(e));
    match kind {
        Kind::File => {
            let init = init.add_subclass(InventoryFile());
            Ok(PyCell::new(py, init)?.to_object(py))
        }
        Kind::Directory => {
            let init = init.add_subclass(InventoryDirectory());
            Ok(PyCell::new(py, init)?.to_object(py))
        }
        Kind::TreeReference => {
            let init = init.add_subclass(TreeReference());
            Ok(PyCell::new(py, init)?.to_object(py))
        }
        Kind::Symlink => {
            let init = init.add_subclass(InventoryLink());
            Ok(PyCell::new(py, init)?.to_object(py))
        }
    }
}

#[pyfunction]
fn make_entry(
    py: Python,
    kind: &str,
    name: &str,
    parent_id: Option<&[u8]>,
    file_id: Option<&[u8]>,
) -> PyResult<PyObject> {
    let kind = match kind {
        "file" => Kind::File,
        "directory" => Kind::Directory,
        "tree-reference" => Kind::TreeReference,
        "symlink" => Kind::Symlink,
        _ => panic!("Unknown kind"),
    };
    let parent_id = parent_id.map(FileId::from);
    let file_id = file_id.map_or_else(|| FileId::generate(name), FileId::from);
    entry_to_py(py, Entry::new(kind, name.to_string(), file_id, parent_id))
}

#[pyfunction]
fn is_valid_name(name: &str) -> bool {
    bazaar::inventory::is_valid_name(name)
}

#[pyfunction]
fn ensure_normalized_name(name: std::path::PathBuf) -> PyResult<std::path::PathBuf> {
    bazaar::inventory::ensure_normalized_name(name.as_path())
        .map_err(|_e| InvalidNormalization::new_err(name))
}

#[pyclass]
struct InventoryDelta(bazaar::inventory_delta::InventoryDelta);

#[pymethods]
impl InventoryDelta {
    #[new]
    fn new(
        py: Python,
        delta: Option<
            Vec<(
                Option<String>,
                Option<String>,
                Vec<u8>,
                Option<PyRef<InventoryEntry>>,
            )>,
        >,
    ) -> PyResult<Self> {
        let mut delta = delta.unwrap_or_else(Vec::new);
        let mut delta = delta
            .iter()
            .map(|(old_name, new_name, file_id, entry)| {
                let old_name = old_name.as_ref().map(|s| s.as_str());
                let new_name = new_name.as_ref().map(|s| s.as_str());
                let file_id = file_id.as_slice();
                let entry = entry.as_ref().map(|e| e.0.clone());
                InventoryDeltaEntry {
                    old_path: old_name.map(|s| s.to_string()),
                    new_path: new_name.map(|s| s.to_string()),
                    file_id: FileId::from(file_id),
                    new_entry: entry,
                }
            })
            .collect::<Vec<_>>();
        Ok(Self(bazaar::inventory_delta::InventoryDelta::from(delta)))
    }

    fn __nonzero__(slf: PyRef<Self>) -> bool {
        !slf.0.is_empty()
    }

    fn sort(&mut self) {
        self.0.sort();
    }

    fn __len__(&self) -> usize {
        self.0.len()
    }

    fn __richcmp__(&self, other: PyRef<InventoryDelta>, op: CompareOp) -> PyResult<Option<bool>> {
        match op {
            CompareOp::Eq => Ok(Some(self.0 == other.0)),
            CompareOp::Ne => Ok(Some(self.0 != other.0)),
            _ => Err(PyNotImplementedError::new_err(
                "Only == and != are supported",
            )),
        }
    }

    fn __getitem__(
        &self,
        py: Python,
        index: isize,
    ) -> PyResult<(Option<String>, Option<String>, PyObject, PyObject)> {
        let index: usize = if index < 0 {
            (self.0.len() as isize + index) as usize
        } else {
            index as usize
        };
        let entry = self
            .0
            .get(index)
            .ok_or(PyIndexError::new_err("Index out of bounds"))?;
        Ok((
            entry.old_path.clone(),
            entry.new_path.clone(),
            PyBytes::new(py, entry.file_id.bytes()).to_object(py),
            entry
                .new_entry
                .as_ref()
                .map_or_else(|| Ok(py.None()), |e| entry_to_py(py, e.clone()))?,
        ))
    }

    fn check(&self) -> PyResult<()> {
        self.0.check().map_err(|e| match e {
            InventoryDeltaInconsistency::NoPath => {
                InconsistentDelta::new_err(("", "", "No path in entry"))
            }
            InventoryDeltaInconsistency::DuplicateFileId(ref path, ref fid) => {
                InconsistentDelta::new_err((path.clone(), fid.bytes().to_vec(), "repeated file_id"))
            }
            InventoryDeltaInconsistency::DuplicateOldPath(path, fid) => {
                InconsistentDelta::new_err((path, fid.bytes().to_vec(), "repeated path"))
            }
            InventoryDeltaInconsistency::DuplicateNewPath(path, fid) => {
                InconsistentDelta::new_err((path, fid.bytes().to_vec(), "repeated path"))
            }
            InventoryDeltaInconsistency::MismatchedId(path, fid1, fid2) => {
                InconsistentDelta::new_err((
                    path,
                    fid1.bytes().to_vec(),
                    format!("mismatched id with entry {}", fid2),
                ))
            }
            InventoryDeltaInconsistency::EntryWithoutPath(path, fid) => {
                InconsistentDelta::new_err((path, fid.bytes().to_vec(), "Entry with no new_path"))
            }
            InventoryDeltaInconsistency::PathWithoutEntry(path, fid) => {
                InconsistentDelta::new_err((path, fid.bytes().to_vec(), "new_path with no entry"))
            }
        })
    }

    fn __repr__(&self) -> String {
        format!("{:?}", self.0)
    }
}

#[pyfunction]
fn parse_inventory_delta(
    py: Python,
    lines: Vec<Vec<u8>>,
    allow_versioned_root: Option<bool>,
    allow_tree_references: Option<bool>,
) -> PyResult<(PyObject, PyObject, bool, bool, PyObject)> {
    let (parent, version, versioned_root, tree_references, result) =
        bazaar::inventory_delta::parse_inventory_delta(
            lines
                .iter()
                .map(|x| x.as_slice())
                .collect::<Vec<_>>()
                .as_slice(),
            allow_versioned_root,
            allow_tree_references,
        )
        .map_err(|e| match e {
            InventoryDeltaParseError::Invalid(m) => InventoryDeltaError::new_err((m,)),
            InventoryDeltaParseError::Incompatible(m) => IncompatibleInventoryDelta::new_err((m,)),
        })?;

    let parent = PyBytes::new(py, parent.bytes()).to_object(py);
    let version = PyBytes::new(py, version.bytes()).to_object(py);

    let result = PyCell::new(py, InventoryDelta(result))?.to_object(py);

    Ok((parent, version, versioned_root, tree_references, result))
}

#[pyfunction]
fn parse_inventory_entry(
    file_id: Vec<u8>,
    name: String,
    parent_id: Option<Vec<u8>>,
    revision: Option<Vec<u8>>,
    lines: &[u8],
) -> InventoryEntry {
    InventoryEntry(bazaar::inventory_delta::parse_inventory_entry(
        FileId::from(file_id),
        name,
        parent_id.map(FileId::from),
        revision.map(RevisionId::from),
        lines,
    ))
}

#[pyfunction]
fn serialize_inventory_entry(py: Python, entry: &InventoryEntry) -> PyResult<PyObject> {
    Ok(PyBytes::new(
        py,
        bazaar::inventory_delta::serialize_inventory_entry(&entry.0)
            .map_err(|e| match e {
                InventoryDeltaSerializeError::Invalid(m) => InventoryDeltaError::new_err((m,)),
                InventoryDeltaSerializeError::UnsupportedKind(k) => PyKeyError::new_err((k,)),
            })?
            .as_slice(),
    )
    .to_object(py))
}

#[pyfunction]
fn serialize_inventory_delta(
    py: Python,
    old_name: Vec<u8>,
    new_name: Vec<u8>,
    delta_to_new: &InventoryDelta,
    versioned_root: bool,
    tree_references: bool,
) -> PyResult<Vec<PyObject>> {
    Ok(bazaar::inventory_delta::serialize_inventory_delta(
        &RevisionId::from(old_name),
        &RevisionId::from(new_name),
        &delta_to_new.0,
        versioned_root,
        tree_references,
    )
    .map_err(|e| match e {
        InventoryDeltaSerializeError::Invalid(m) => InventoryDeltaError::new_err((m,)),
        InventoryDeltaSerializeError::UnsupportedKind(m) => PyKeyError::new_err((m,)),
    })?
    .into_iter()
    .map(|x| PyBytes::new(py, x.as_slice()).to_object(py))
    .collect())
}

pub fn _inventory_rs(py: Python) -> PyResult<&PyModule> {
    let m = PyModule::new(py, "inventory")?;

    m.add_class::<InventoryEntry>()?;
    m.add_class::<InventoryFile>()?;
    m.add_class::<InventoryLink>()?;
    m.add_class::<InventoryDirectory>()?;
    m.add_class::<TreeReference>()?;
    m.add_wrapped(wrap_pyfunction!(make_entry))?;
    m.add_wrapped(wrap_pyfunction!(is_valid_name))?;
    m.add_wrapped(wrap_pyfunction!(ensure_normalized_name))?;
    m.add_class::<InventoryDelta>()?;
    m.add_wrapped(wrap_pyfunction!(parse_inventory_delta))?;
    m.add_wrapped(wrap_pyfunction!(parse_inventory_entry))?;
    m.add_wrapped(wrap_pyfunction!(serialize_inventory_delta))?;
    m.add_wrapped(wrap_pyfunction!(serialize_inventory_entry))?;
    m.add("InventoryDeltaError", py.get_type::<InventoryDeltaError>())?;
    m.add(
        "IncompatibleInventoryDelta",
        py.get_type::<IncompatibleInventoryDelta>(),
    )?;

    Ok(m)
}
