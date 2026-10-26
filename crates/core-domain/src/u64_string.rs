//! JSON counters use decimal strings so JavaScript cannot round them.

use serde::{Deserialize, Deserializer, Serializer};

pub fn serialize<S: Serializer>(value: &u64, serializer: S) -> Result<S::Ok, S::Error> {
    serializer.serialize_str(&value.to_string())
}

pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<u64, D::Error> {
    let value = String::deserialize(deserializer)?;
    if value.is_empty()
        || !value.bytes().all(|byte| byte.is_ascii_digit())
        || (value.len() > 1 && value.starts_with('0'))
    {
        return Err(serde::de::Error::custom(
            "expected canonical unsigned decimal string",
        ));
    }
    value.parse().map_err(serde::de::Error::custom)
}
