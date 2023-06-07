#[cfg(feature = "i18n")]
pub mod i18n;

#[cfg(not(feature = "i18n"))]
pub mod i18n {
    pub fn gettext(msgid: &str) -> String {
        msgid.to_string()
    }

    pub fn nggettext(msgid: &str, msgid_plural: &str, n: usize) -> String {
        if n == 1 {
            msgid.to_string()
        } else {
            msgid_plural.to_string()
        }
    }
}

pub mod bedding;
pub mod branch;
pub mod controldir;
pub mod forge;
pub mod location;
pub mod progress;
pub mod repository;
pub mod tags;
pub mod trace;
pub mod tree;

#[cfg(feature = "pyo3")]
pub mod pytree;

#[cfg(feature = "pyo3")]
pub mod pybranch;

#[cfg(feature = "pyo3")]
pub mod pyforge;

#[cfg(feature = "pyo3")]
pub mod pytags;

#[cfg(feature = "pyo3")]
pub mod pycontroldir;
