//! Retail music player: station playlists converted from the disc.
//!
//! The setup pipeline writes `assets/private/audio/music.json` with one
//! track list per station. This plugin shuffles the selected station and
//! advances when the sink runs dry. A missing or outdated manifest leaves
//! the player idle; music is optional content, never a startup failure.
use bevy::{audio::{PlaybackMode, Volume}, prelude::*};
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
};

/// Menu order for `GraphicsSettings::music`; the last entry mutes.
pub(crate) const STATIONS: &[&str] = &["World", "Game", "iPod", "Off"];
const GAIN: f32 = 0.5;

#[derive(Debug, Clone, serde::Deserialize)]
struct Track {
    file: String,
    #[allow(dead_code)]
    index: usize,
    #[allow(dead_code)]
    samples: u64,
    #[allow(dead_code)]
    rate: u32,
    #[allow(dead_code)]
    channels: u8,
    #[allow(dead_code)]
    duration_s: f32,
}
#[derive(Debug, Clone, Default, serde::Deserialize)]
struct Station {
    #[serde(default)]
    tracks: Vec<Track>,
}
#[derive(Debug, Clone, serde::Deserialize)]
struct Manifest {
    version: u32,
    #[serde(default)]
    stations: HashMap<String, Station>,
}

#[derive(Resource, Default)]
struct Music {
    stations: HashMap<String, Vec<PathBuf>>,
    order: Vec<usize>,
    pos: usize,
    station: u8,
    player: Option<Entity>,
}

pub(crate) struct MusicPlugin;
impl Plugin for MusicPlugin {
    fn build(&self, app: &mut App) {
        app.init_resource::<Music>()
            .add_systems(Startup, load)
            .add_systems(Update, play);
    }
}

fn manifest_path(assets: &Path) -> PathBuf {
    assets.join("private/audio/music.json")
}

fn load_manifest(assets: &Path) -> Result<Manifest, String> {
    let bytes =
        std::fs::read(manifest_path(assets)).map_err(|e| format!("Music manifest: {e}"))?;
    let manifest: Manifest =
        serde_json::from_slice(&bytes).map_err(|e| format!("Music manifest: {e}"))?;
    if manifest.version != 1 {
        return Err("Unsupported music manifest version".into());
    }
    Ok(manifest)
}

/// Fisher-Yates with xorshift64*; no extra dependencies for a shuffle.
fn shuffled(count: usize, mut seed: u64) -> Vec<usize> {
    let mut order: Vec<usize> = (0..count).collect();
    for i in (1..count).rev() {
        seed ^= seed >> 12;
        seed ^= seed << 25;
        seed ^= seed >> 27;
        let j = (seed.wrapping_mul(0x2545F4914F6CDD1D) >> 33) as usize % (i + 1);
        order.swap(i, j);
    }
    order
}

fn load(config: Res<crate::config::Config>, mut music: ResMut<Music>) {
    let manifest = match load_manifest(&config.asset_root) {
        Ok(manifest) => manifest,
        Err(error) => {
            warn!("{error}; continuing without retail music");
            return;
        }
    };
    for (name, station) in &manifest.stations {
        music.stations.insert(
            name.clone(),
            station
                .tracks
                .iter()
                .map(|track| PathBuf::from("private/audio").join(&track.file))
                .collect(),
        );
    }
}

fn station_key(setting: u8) -> Option<&'static str> {
    match setting {
        0 => Some("world"),
        1 => Some("game"),
        2 => Some("ipod"),
        _ => None,
    }
}

fn play(
    mut commands: Commands,
    asset_server: Res<AssetServer>,
    menu: Option<Res<crate::graphics_menu::Menu>>,
    time: Res<Time<Real>>,
    mut music: ResMut<Music>,
    entities: Query<Entity>,
    sinks: Query<&AudioSink>,
) {
    let wanted = menu.map_or(0, |menu| menu.music_station());
    let key = station_key(wanted);
    if music.station != wanted {
        music.station = wanted;
        if let Some(entity) = music.player.take() {
            commands.entity(entity).despawn();
        }
        music.order.clear();
        music.pos = 0;
    }
    let Some(key) = key else {
        return;
    };
    let count = music.stations.get(key).map_or(0, |tracks| tracks.len());
    if count == 0 {
        return;
    }
    let finished = match music.player {
        None => true,
        // A missing sink only means playback has not started yet: waiting
        // here instead of respinning is what keeps headless and silent
        // machines to one idle voice instead of thousands per minute.
        Some(entity) => {
            entities.get(entity).is_err()
                || sinks
                    .get(entity)
                    .map(|sink| sink.empty())
                    .unwrap_or(false)
        }
    };
    if !finished {
        return;
    }
    if let Some(entity) = music.player.take() {
        commands.entity(entity).despawn();
    }
    if music.order.len() != count {
        let seed = time.elapsed().as_nanos() as u64 ^ 0x9E3779B97F4A7C15;
        music.order = shuffled(count, seed.max(1));
        music.pos = 0;
    }
    let track = music.stations[key][music.order[music.pos % music.order.len()]].clone();
    music.pos += 1;
    info!("Music: playing {} ({} of {})", track.display(), music.pos, music.order.len());
    music.player = Some(
        commands
            .spawn((
                AudioPlayer::new(asset_server.load::<AudioSource>(track.clone())),
                PlaybackSettings {
                    mode: PlaybackMode::Once,
                    volume: Volume::Linear(GAIN),
                    ..default()
                },
            ))
            .id(),
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn station_keys_cover_menu_settings() {
        assert_eq!(STATIONS, &["World", "Game", "iPod", "Off"]);
        assert_eq!(station_key(0), Some("world"));
        assert_eq!(station_key(1), Some("game"));
        assert_eq!(station_key(2), Some("ipod"));
        assert_eq!(station_key(3), None);
        assert_eq!(station_key(9), None);
    }
    #[test]
    fn manifest_parses_and_rejects_versions() {
        let manifest: Manifest = serde_json::from_str(
            r#"{"version":1,"stations":{"world":{"tracks":[
                {"file":"world/0001.ogg","index":1,"samples":70560,"rate":44100,"channels":2,"duration_s":1.6}],
                "count":1}}}"#,
        )
        .unwrap();
        assert_eq!(manifest.version, 1);
        assert_eq!(manifest.stations["world"].tracks.len(), 1);
        assert_eq!(manifest.stations["world"].tracks[0].file, "world/0001.ogg");
        let bad: Result<Manifest, _> = serde_json::from_str(r#"{"version":1}"#);
        assert!(bad.unwrap().stations.is_empty());
    }
    #[test]
    fn shuffle_is_a_deterministic_permutation() {
        for seed in [1u64, 7, 0x9E3779B97F4A7C15] {
            let mut order = shuffled(64, seed);
            order.sort_unstable();
            assert_eq!(order, (0..64).collect::<Vec<_>>());
        }
        assert_eq!(shuffled(64, 7), shuffled(64, 7));
        assert_ne!(shuffled(64, 7), shuffled(64, 8));
        assert!(shuffled(0, 1).is_empty());
    }
    #[test]
    fn missing_manifest_is_not_an_error() {
        assert!(load_manifest(Path::new("nonexistent-assets-dir")).is_err());
    }
}
