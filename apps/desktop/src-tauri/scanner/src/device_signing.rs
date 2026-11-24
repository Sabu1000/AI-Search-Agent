//! Offline Ed25519 proof construction and verification for desktop routes.
//! This module does not implement transport, credential lookup, device status,
//! workspace authorization, request-identifier storage, or nonce consumption.

use base64::engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD};
use base64::Engine;
use ed25519_dalek::{Signature, Signer, SigningKey, VerifyingKey};
use sha2::{Digest, Sha256};
use time::format_description::well_known::Rfc3339;
use time::{Duration, OffsetDateTime};
use uuid::Uuid;
use zeroize::Zeroizing;

const REGISTRATION_DOMAIN: &str = "uas-device-registration-v1";
const REQUEST_DOMAIN: &str = "uas-device-request-v1";
const TIMESTAMP_TOLERANCE: Duration = Duration::minutes(5);
pub const MAX_SIGNED_BODY_BYTES: usize = 2_000_000;

/// Errors intentionally contain no private keys, proof material, or body data.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProofError {
    InvalidMessage,
    InvalidSignature,
    TimestampOutOfWindow,
    EntropyUnavailable,
}

/// Proofs of possession are not credentials. Keep them out of logs, because a
/// registration challenge may still be live while a proof is in transit.
pub struct RegistrationProof {
    pub public_key: String,
    pub signature: String,
}

/// Header values for an exact device request body. Authorization is supplied
/// separately by the transport; never log any of these values with it.
pub struct DeviceHeaders {
    pub device_timestamp: String,
    pub device_nonce: String,
    pub content_digest: String,
    pub device_signature: String,
    pub request_id: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeviceRoute {
    Heartbeat,
    Manifests,
    SignUpload,
    Changes,
}

impl DeviceRoute {
    fn segment(self) -> &'static str {
        match self {
            Self::Heartbeat => "heartbeat",
            Self::Manifests => "manifests",
            Self::SignUpload => "uploads:sign",
            Self::Changes => "changes",
        }
    }

    pub fn canonical_path(self, device_id: Uuid) -> Result<String, ProofError> {
        if device_id.is_nil() {
            return Err(ProofError::InvalidMessage);
        }
        Ok(format!("/v1/devices/{device_id}/{}", self.segment()))
    }

    fn check_canonical_path(path: &str) -> Result<(), ProofError> {
        if path.len() > 80 {
            return Err(ProofError::InvalidMessage);
        }
        let (device_id, endpoint) = path
            .strip_prefix("/v1/devices/")
            .and_then(|tail| tail.split_once('/'))
            .ok_or(ProofError::InvalidMessage)?;
        parse_uuid(device_id)?;
        if !matches!(
            endpoint,
            "heartbeat" | "manifests" | "uploads:sign" | "changes"
        ) {
            return Err(ProofError::InvalidMessage);
        }
        Ok(())
    }
}

/// The seed is never retained by the caller of generate(), logged, or
/// persisted by this crate. OS-keychain storage/import is a separate task.
pub struct DeviceSigningKey(SigningKey);

impl DeviceSigningKey {
    pub fn generate() -> Result<Self, ProofError> {
        let mut seed = Zeroizing::new([0; 32]);
        getrandom::fill(&mut *seed).map_err(|_| ProofError::EntropyUnavailable)?;
        Ok(Self::from_seed(&seed))
    }

    /// Import only a device seed that the trusted desktop service has obtained
    /// securely. Test vectors use an explicit nonsecret seed.
    pub fn from_seed(seed: &Zeroizing<[u8; 32]>) -> Self {
        Self(SigningKey::from_bytes(seed))
    }

    pub fn public_key(&self) -> String {
        URL_SAFE_NO_PAD.encode(self.0.verifying_key().as_bytes())
    }

    pub fn sign_registration(
        &self,
        challenge_id: Uuid,
        challenge: &[u8; 32],
    ) -> Result<RegistrationProof, ProofError> {
        let public_key = self.public_key();
        let message = registration_message(challenge_id, challenge, &public_key)?;
        let signature = self.0.sign(message.as_bytes());
        Ok(RegistrationProof {
            public_key,
            signature: URL_SAFE_NO_PAD.encode(signature.to_bytes()),
        })
    }

    pub fn sign_request(
        &self,
        route: DeviceRoute,
        device_id: Uuid,
        timestamp: &str,
        request_id: Uuid,
        exact_body: &[u8],
    ) -> Result<DeviceHeaders, ProofError> {
        let mut nonce = [0; 16];
        getrandom::fill(&mut nonce).map_err(|_| ProofError::EntropyUnavailable)?;
        self.sign_request_with_nonce(route, device_id, timestamp, request_id, exact_body, nonce)
    }

    fn sign_request_with_nonce(
        &self,
        route: DeviceRoute,
        device_id: Uuid,
        timestamp: &str,
        request_id: Uuid,
        exact_body: &[u8],
        nonce: [u8; 16],
    ) -> Result<DeviceHeaders, ProofError> {
        let path = route.canonical_path(device_id)?;
        check_timestamp(timestamp)?;
        if request_id.is_nil() {
            return Err(ProofError::InvalidMessage);
        }
        let request_id = request_id.to_string();
        let content_digest = content_digest(exact_body)?;
        let device_nonce = URL_SAFE_NO_PAD.encode(nonce);
        let message = request_message(
            &path,
            timestamp,
            &device_nonce,
            &content_digest,
            &request_id,
        );
        let signature = self.0.sign(message.as_bytes());
        Ok(DeviceHeaders {
            device_timestamp: timestamp.into(),
            device_nonce,
            content_digest,
            device_signature: URL_SAFE_NO_PAD.encode(signature.to_bytes()),
            request_id,
        })
    }
}

/// Does not validate server challenge age, ownership, or single-use state.
/// The eventual registration handler must check and consume that state.
pub fn verify_registration(
    proof: &RegistrationProof,
    challenge_id: Uuid,
    challenge: &[u8; 32],
) -> Result<(), ProofError> {
    let message = registration_message(challenge_id, challenge, &proof.public_key)?;
    verify(&proof.public_key, &proof.signature, message.as_bytes())
}

/// Check the exact signing contract only. The server must also resolve the
/// credential to an active registered device and atomically consume the nonce.
/// Device endpoints use POST, canonical allowlisted paths and no query string.
pub fn verify_request(
    registered_public_key: &str,
    method: &str,
    canonical_path: &str,
    headers: &DeviceHeaders,
    exact_body: &[u8],
    now: OffsetDateTime,
) -> Result<(), ProofError> {
    if method != "POST" {
        return Err(ProofError::InvalidMessage);
    }
    DeviceRoute::check_canonical_path(canonical_path)?;
    parse_uuid(&headers.request_id)?;
    let timestamp = check_timestamp(&headers.device_timestamp)?;
    if (timestamp - now).abs() > TIMESTAMP_TOLERANCE {
        return Err(ProofError::TimestampOutOfWindow);
    }
    if !valid_nonce(&headers.device_nonce) || content_digest(exact_body)? != headers.content_digest
    {
        return Err(ProofError::InvalidMessage);
    }
    let message = request_message(
        canonical_path,
        &headers.device_timestamp,
        &headers.device_nonce,
        &headers.content_digest,
        &headers.request_id,
    );
    verify(
        registered_public_key,
        &headers.device_signature,
        message.as_bytes(),
    )
}

fn parse_uuid(value: &str) -> Result<Uuid, ProofError> {
    if value.len() != 36 {
        return Err(ProofError::InvalidMessage);
    }
    let parsed = Uuid::parse_str(value).map_err(|_| ProofError::InvalidMessage)?;
    if parsed.is_nil() || parsed.hyphenated().to_string() != value {
        return Err(ProofError::InvalidMessage);
    }
    Ok(parsed)
}

fn check_timestamp(value: &str) -> Result<OffsetDateTime, ProofError> {
    if value.len() != 20 || value.as_bytes()[10] != b'T' || !value.ends_with('Z') {
        return Err(ProofError::InvalidMessage);
    }
    let parsed = OffsetDateTime::parse(value, &Rfc3339).map_err(|_| ProofError::InvalidMessage)?;
    if parsed.nanosecond() != 0 || parsed.year() < 1 {
        return Err(ProofError::InvalidMessage);
    }
    Ok(parsed)
}

fn content_digest(body: &[u8]) -> Result<String, ProofError> {
    if body.len() > MAX_SIGNED_BODY_BYTES {
        return Err(ProofError::InvalidMessage);
    }
    Ok(format!(
        "sha-256=:{}:",
        STANDARD.encode(Sha256::digest(body))
    ))
}

fn valid_nonce(encoded: &str) -> bool {
    (22..=43).contains(&encoded.len())
        && URL_SAFE_NO_PAD.decode(encoded).is_ok_and(|bytes| {
            (16..=32).contains(&bytes.len()) && URL_SAFE_NO_PAD.encode(bytes) == encoded
        })
}

fn decode_exact<const N: usize>(input: &str) -> Result<[u8; N], ProofError> {
    if input.len() != (N * 8).div_ceil(6) {
        return Err(ProofError::InvalidMessage);
    }
    let bytes = URL_SAFE_NO_PAD
        .decode(input)
        .map_err(|_| ProofError::InvalidMessage)?;
    if URL_SAFE_NO_PAD.encode(&bytes) != input {
        return Err(ProofError::InvalidMessage);
    }
    bytes.try_into().map_err(|_| ProofError::InvalidMessage)
}

fn registration_message(
    challenge_id: Uuid,
    challenge: &[u8; 32],
    public_key: &str,
) -> Result<String, ProofError> {
    if challenge_id.is_nil() {
        return Err(ProofError::InvalidMessage);
    }
    let _ = decode_exact::<32>(public_key)?;
    let challenge = URL_SAFE_NO_PAD.encode(challenge);
    Ok(format!(
        "{REGISTRATION_DOMAIN}\n{challenge_id}\n{challenge}\n{public_key}"
    ))
}

fn request_message(
    route_path: &str,
    timestamp: &str,
    nonce: &str,
    digest: &str,
    request_id: &str,
) -> String {
    format!("{REQUEST_DOMAIN}\nPOST\n{route_path}\n{timestamp}\n{nonce}\n{digest}\n{request_id}")
}

fn verify(public_key: &str, signature: &str, message: &[u8]) -> Result<(), ProofError> {
    let public_key = VerifyingKey::from_bytes(&decode_exact(public_key)?)
        .map_err(|_| ProofError::InvalidSignature)?;
    let signature = Signature::from_bytes(&decode_exact(signature)?);
    public_key
        .verify_strict(message, &signature)
        .map_err(|_| ProofError::InvalidSignature)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn vector() -> Value {
        serde_json::from_str(include_str!("../fixtures/ed25519-v1.json")).unwrap()
    }

    fn text<'value>(data: &'value Value, field: &str) -> &'value str {
        data[field].as_str().unwrap()
    }

    fn fixed_bytes<const N: usize>(hex: &str) -> [u8; N] {
        assert_eq!(hex.len(), N * 2);
        std::array::from_fn(|i| u8::from_str_radix(&hex[i * 2..i * 2 + 2], 16).unwrap())
    }

    fn fixed_key(data: &Value) -> DeviceSigningKey {
        DeviceSigningKey::from_seed(&Zeroizing::new(fixed_bytes(text(data, "seed_hex"))))
    }

    fn registration(data: &Value, key: &DeviceSigningKey) -> RegistrationProof {
        let registration = &data["registration"];
        let challenge_id = parse_uuid(text(registration, "challenge_id")).unwrap();
        key.sign_registration(
            challenge_id,
            &fixed_bytes(text(registration, "challenge_hex")),
        )
        .unwrap()
    }

    fn headers(data: &Value, key: &DeviceSigningKey) -> DeviceHeaders {
        key.sign_request_with_nonce(
            DeviceRoute::Manifests,
            parse_uuid(text(data, "device_id")).unwrap(),
            text(data, "timestamp"),
            parse_uuid(text(data, "request_id")).unwrap(),
            text(data, "body").as_bytes(),
            fixed_bytes(text(data, "nonce_hex")),
        )
        .unwrap()
    }

    #[test]
    fn independent_registration_and_request_vectors_match_exact_bytes() {
        let data = vector();
        let key = fixed_key(&data);
        assert_eq!(key.public_key(), text(&data, "public_key"));
        let proof = registration(&data, &key);
        let register = &data["registration"];
        assert_eq!(proof.signature, text(register, "signature"));
        let challenge = fixed_bytes(text(register, "challenge_hex"));
        let challenge_id = parse_uuid(text(register, "challenge_id")).unwrap();
        assert_eq!(
            registration_message(challenge_id, &challenge, &proof.public_key).unwrap(),
            text(register, "message")
        );
        assert_eq!(
            verify_registration(&proof, challenge_id, &challenge),
            Ok(())
        );
        assert_eq!(
            verify_registration(&proof, challenge_id, &[13; 32]),
            Err(ProofError::InvalidSignature)
        );
        assert_eq!(
            verify_registration(&proof, Uuid::nil(), &challenge),
            Err(ProofError::InvalidMessage)
        );

        let request = &data["request"];
        let headers = headers(request, &key);
        assert_eq!(headers.device_nonce, text(request, "nonce"));
        assert_eq!(headers.content_digest, text(request, "content_digest"));
        assert_eq!(headers.device_signature, text(request, "signature"));
        let path = text(request, "path");
        assert_eq!(
            request_message(
                path,
                &headers.device_timestamp,
                &headers.device_nonce,
                &headers.content_digest,
                &headers.request_id
            ),
            text(request, "message")
        );
        let now = check_timestamp(text(request, "timestamp")).unwrap();
        let body = text(request, "body").as_bytes();
        assert_eq!(
            verify_request(&key.public_key(), "POST", path, &headers, body, now),
            Ok(())
        );
    }

    #[test]
    fn tampered_headers_path_or_body_fail_verification() {
        let data = vector();
        let key = fixed_key(&data);
        let request = &data["request"];
        let path = text(request, "path");
        let mut fields = headers(request, &key);
        let now = check_timestamp(text(request, "timestamp")).unwrap();
        let body = text(request, "body").as_bytes();
        let verify = |method: &str, path: &str, fields: &DeviceHeaders, body: &[u8]| {
            verify_request(&key.public_key(), method, path, fields, body, now)
        };
        assert_eq!(
            verify("GET", path, &fields, body),
            Err(ProofError::InvalidMessage)
        );
        let query_path = format!("{path}?any=1");
        assert_eq!(
            verify("POST", &query_path, &fields, body),
            Err(ProofError::InvalidMessage)
        );
        let alternate_path = path.replace("/manifests", "/changes");
        assert_eq!(
            verify("POST", &alternate_path, &fields, body),
            Err(ProofError::InvalidSignature)
        );
        assert_eq!(
            verify("POST", path, &fields, b"{}"),
            Err(ProofError::InvalidMessage)
        );
        fields.request_id = "018f4fa3-3d41-7486-a5dc-f49b976c2410".into();
        assert_eq!(
            verify("POST", path, &fields, body),
            Err(ProofError::InvalidSignature)
        );
        fields.device_nonce = "x".into();
        assert_eq!(
            verify("POST", path, &fields, body),
            Err(ProofError::InvalidMessage)
        );
    }

    #[test]
    fn enforces_canonical_timestamps_and_exact_freshness() {
        let data = vector();
        let key = fixed_key(&data);
        let request = &data["request"];
        let path = text(request, "path");
        let headers = headers(request, &key);
        let now = check_timestamp(text(request, "timestamp")).unwrap();
        let body = text(request, "body").as_bytes();
        let verify_at = |t| verify_request(&key.public_key(), "POST", path, &headers, body, t);
        assert_eq!(verify_at(now - TIMESTAMP_TOLERANCE), Ok(()));
        assert_eq!(verify_at(now + TIMESTAMP_TOLERANCE), Ok(()));
        assert_eq!(
            verify_at(now + TIMESTAMP_TOLERANCE + Duration::nanoseconds(1)),
            Err(ProofError::TimestampOutOfWindow)
        );
        assert_eq!(
            verify_at(now - TIMESTAMP_TOLERANCE - Duration::nanoseconds(1)),
            Err(ProofError::TimestampOutOfWindow)
        );
        for invalid in [
            "2026-02-29T00:00:00Z",
            "2026-11-24T17:45:00z",
            "2026-11-24T17:45:00+00:00",
            "2026-11-24T17:45:00.00Z",
            "2026-11-24t17:45:00Z",
        ] {
            assert_eq!(check_timestamp(invalid), Err(ProofError::InvalidMessage));
        }
        assert_eq!(
            content_digest(&vec![0; MAX_SIGNED_BODY_BYTES + 1]),
            Err(ProofError::InvalidMessage)
        );
    }

    #[test]
    fn generated_keys_remain_ephemeral_and_request_uses_random_nonce() {
        let key = DeviceSigningKey::generate().unwrap();
        let data = vector();
        let request = &data["request"];
        let device_id = parse_uuid(text(request, "device_id")).unwrap();
        let request_id = parse_uuid(text(request, "request_id")).unwrap();
        let timestamp = text(request, "timestamp");
        let first = key
            .sign_request(
                DeviceRoute::Heartbeat,
                device_id,
                timestamp,
                request_id,
                b"",
            )
            .unwrap();
        let second = key
            .sign_request(
                DeviceRoute::Heartbeat,
                device_id,
                timestamp,
                request_id,
                b"",
            )
            .unwrap();
        assert_eq!(
            URL_SAFE_NO_PAD.decode(&first.device_nonce).unwrap().len(),
            16
        );
        assert_ne!(first.device_nonce, second.device_nonce);
        assert_ne!(first.device_signature, second.device_signature);
        assert!(DeviceRoute::check_canonical_path(
            "/v1/devices/00000000-0000-0000-0000-000000000000/heartbeat"
        )
        .is_err());
    }
}
