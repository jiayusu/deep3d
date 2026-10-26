//! Pure typed engineering state, validation and deterministic feature compilation.

mod compiler;
mod model;
mod transaction;
pub mod u64_string;
mod validation;

pub use model::*;
pub use transaction::{apply_transaction, dirty_features};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, thiserror::Error)]
#[serde(deny_unknown_fields)]
#[error("{code}: {message}")]
pub struct CoreError {
    pub code: String,
    pub message: String,
}

impl CoreError {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
        }
    }
}

pub type Result<T> = std::result::Result<T, CoreError>;

pub fn canonical_bytes<T: Serialize>(value: &T) -> Result<Vec<u8>> {
    serde_jcs::to_vec(value)
        .map_err(|error| CoreError::new("SERIALIZATION_FAILED", error.to_string()))
}

pub fn digest_bytes(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}
