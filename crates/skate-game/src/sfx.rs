//! Retail sound effects: rolling, grind and bail loops plus impact one-shots.
//!
//! The setup pipeline writes `assets/private/audio/sfx.json` with bank clips,
//! ambience beds and wheel stock converted from the disc. Bank roles below
//! are name-matched heuristics, not the retail event graph. A missing
//! manifest idles silently; sound effects are optional content.
use bevy::{audio::{AudioSink, PlaybackMode, Volume}, prelude::*};
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
};

const ROLL_GAIN: f32 = 0.45;
const GRIND_GAIN: f32 = 0.5;
const LAND_GAIN: f32 = 0.7;
const BAIL_GAIN: f32 = 0.5;
const AMBIENT_GAIN: f32 = 0.35;

/// Bank stems by role. Landing reuses board scrapes: touchdown on concrete
/// is a slap, and no dedicated landing bank has been identified yet.
const GRIND_BANK: &str = "GRINDS";
const LAND_BANK: &str = "board_scrapes";
const BAIL_BANK: &str = "Bodyslide";

#[derive(Debug, Clone, serde::Deserialize)]
struct Clip {
    file: String,
    #[allow(dead_code)]
    index: Option<usize>,
    #[allow(dead_code)]
    samples: u64,
    #[allow(dead_code)]
    rate: u32,
    #[allow(dead_code)]
    channels: u8,
    #[allow(dead_code)]
    #[serde(rename = "loop", default)]
    loop_: bool,
    #[allow(dead_code)]
    duration_s: f32,
}

#[derive(Debug, Clone, Default, serde::Deserialize)]
struct Manifest {
    version: u32,
    #[serde(default)]
    banks: HashMap<String, Vec<Clip>>,
    #[serde(default)]
    ambience: HashMap<String, Clip>,
    #[serde(default)]
    wheels: Vec<Clip>,
}

#[derive(Resource, Default)]
struct Sfx {
    banks: HashMap<String, Vec<Handle<AudioSource>>>,
    beds: Vec<(String, Handle<AudioSource>)>,
    wheels: Vec<Handle<AudioSource>>,
    rolling: Option<Entity>,
    grinding: Option<Entity>,
    bail_sliding: Option<Entity>,
    ambient: Option<Entity>,
    ambient_bed: String,
    grind_index: usize,
    land_index: usize,
    was_grinding: bool,
    was_bailing: bool,
    landing_seq: u32,
    fall_speed: f32,
    voices: Vec<(Entity, f32)>,
}
/// District beds keyed by map file stem fragments, first match wins.
const MAP_BEDS: &[(&str, &str)] = &[
    ("university", "univ"),
    ("downtown", "dt"),
    ("industrial", "indu"),
    ("skateschool", "skate_school"),
    ("blackbox", "space_park"),
    ("maloof", "dt"),
    ("mega", "univ"),
    ("start", "univ"),
];

fn bed_for_map(map_path: Option<&Path>, beds: &[(String, Handle<AudioSource>)]) -> Option<(String, Handle<AudioSource>)> {
    let stem = map_path
        .and_then(|path| path.file_stem())
        .map(|stem| stem.to_string_lossy().to_lowercase())
        .unwrap_or_default();
    if let Some(want) = MAP_BEDS
        .iter()
        .find(|(key, _)| stem.contains(key))
        .map(|(_, bed)| *bed)
    {
        if let Some(found) = beds.iter().find(|(name, _)| name.contains(want)) {
            return Some(found.clone());
        }
    }
    beds.first().cloned()
}

pub(crate) struct SfxPlugin;
impl Plugin for SfxPlugin {
    fn build(&self, app: &mut App) {
        app.init_resource::<Sfx>()
            .add_systems(Startup, load)
            .add_systems(Update, direct);
    }
}

fn load(
    config: Res<crate::config::Config>,
    asset_server: Res<AssetServer>,
    mut sfx: ResMut<Sfx>,
) {
    let path = config.asset_root.join("private/audio/sfx.json");
    let manifest: Manifest = match std::fs::read(&path) {
        Ok(bytes) => match serde_json::from_slice::<Manifest>(&bytes) {
            Ok(manifest) if manifest.version == 1 => manifest,
            _ => {
                warn!("Unsupported sound-effects manifest; continuing without retail sound effects");
                return;
            }
        },
        Err(error) => {
            warn!("{error}; continuing without retail sound effects");
            return;
        }
    };
    for (bank, clips) in &manifest.banks {
        sfx.banks.insert(
            bank.clone(),
            clips
                .iter()
                .map(|clip| asset_server.load::<AudioSource>(asset_audio(&clip.file)))
                .collect(),
        );
    }
    let mut beds: Vec<(String, Handle<AudioSource>)> = manifest
        .ambience
        .iter()
        .map(|(stem, clip)| (stem.clone(), asset_server.load::<AudioSource>(asset_audio(&clip.file))))
        .collect();
    beds.sort_by(|a, b| a.0.cmp(&b.0));
    sfx.beds = beds;
    sfx.wheels = manifest
        .wheels
        .iter()
        .map(|clip| asset_server.load::<AudioSource>(asset_audio(&clip.file)))
        .collect();
}

fn asset_audio(file: &str) -> PathBuf {
    PathBuf::from("private/audio").join(file)
}

fn loop_voice(
    world: &mut World,
    handle: &Handle<AudioSource>,
    gain: f32,
) -> Entity {
    world
        .spawn((
            AudioPlayer::new(handle.clone()),
            PlaybackSettings {
                mode: PlaybackMode::Loop,
                volume: Volume::Linear(gain),
                ..default()
            },
        ))
        .id()
}

fn one_shot(
    world: &mut World,
    handle: &Handle<AudioSource>,
    gain: f32,
    pitch: f32,
) -> Entity {
    world
        .spawn((
            AudioPlayer::new(handle.clone()),
            PlaybackSettings {
                mode: PlaybackMode::Once,
                volume: Volume::Linear(gain),
                speed: pitch,
                ..default()
            },
        ))
        .id()
}

fn set_gain(world: &mut World, entity: Entity, gain: f32) {
    if let Some(mut settings) = world.get_mut::<PlaybackSettings>(entity) {
        settings.volume = Volume::Linear(gain);
    }
    if let Some(mut sink) = world.get_mut::<AudioSink>(entity) {
        sink.set_volume(Volume::Linear(gain));
    }
}

fn stop_voice(world: &mut World, slot: &mut Option<Entity>) {
    if let Some(entity) = slot.take() {
        world.despawn(entity);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn bed_mapping_prefers_districts_and_falls_back() {
        let beds = vec![
            ("08_univ_mt_low".to_string(), Handle::<AudioSource>::default()),
            ("04_dt_main".to_string(), Handle::<AudioSource>::default()),
            ("11_indu_shipyard".to_string(), Handle::<AudioSource>::default()),
        ];
        let pick = |map: &str| {
            bed_for_map(Some(Path::new(map)), &beds).map(|(name, _)| name)
        };
        assert_eq!(pick("University.skate").as_deref(), Some("08_univ_mt_low"));
        assert_eq!(pick("DownTown.skate").as_deref(), Some("04_dt_main"));
        assert_eq!(pick("Industrial.skate").as_deref(), Some("11_indu_shipyard"));
        assert_eq!(pick("MaloofMoneyCup.skate").as_deref(), Some("04_dt_main"));
        assert_eq!(pick("Unknown.skate").as_deref(), Some("08_univ_mt_low"));
        assert!(bed_for_map(None, &beds).is_some());
        let empty: Vec<(String, Handle<AudioSource>)> = Vec::new();
        assert!(bed_for_map(Some(Path::new("University.skate")), &empty).is_none());
    }
    #[test]
    fn bank_roles_are_named() {
        assert_eq!((GRIND_BANK, LAND_BANK, BAIL_BANK), ("GRINDS", "board_scrapes", "Bodyslide"));
    }
}

fn direct(world: &mut World) {
    let speed;
    let grinding;
    let bailing;
    let airborne;
    let landing_seq;
    let fall;
    let map_path;
    let menu_open;
    {
        let skater = world.resource::<crate::physics::SkaterRuntime>();
        let input = &skater.player_input.physical;
        let raw = input.skateboard.vector_80.map(f32::from_bits);
        speed = (raw[0] * raw[0] + raw[2] * raw[2]).sqrt();
        fall = (-raw[1]).max(0.0);
        grinding = skater.grind.active_name().is_some();
        airborne = input.filtered_state_0 == 2;
        landing_seq = skater.scoring.landing_seq;
        let physics = world.resource::<crate::physics::GamePhysics>();
        bailing = physics.board_wiping_out;
        map_path = world
            .resource::<crate::config::Config>()
            .map_path
            .clone();
        menu_open = world
            .get_resource::<crate::graphics_menu::Menu>()
            .is_some_and(|menu| menu.open);
    }
    world.resource_scope(|world, mut sfx: Mut<Sfx>| {
        // Rolling loop follows horizontal speed; silent in the air and slow.
        let roll_gain = if menu_open || airborne {
            0.0
        } else {
            ((speed - 0.5) / 8.0).clamp(0.0, 1.0) * ROLL_GAIN
        };
        if let Some(wheel) = sfx.wheels.last() {
            if sfx.rolling.is_none() {
                let entity = loop_voice(world, wheel, 0.0);
                sfx.rolling = Some(entity);
            }
            if let Some(entity) = sfx.rolling {
                set_gain(world, entity, roll_gain);
            }
        }
        // Grind loop while a grind is active.
        if grinding && !sfx.was_grinding {
            if let Some(clip) = sfx.banks.get(GRIND_BANK).and_then(|bank| {
                (!bank.is_empty()).then(|| bank[(sfx.grind_index + 1) % bank.len()].clone())
            }) {
                sfx.grind_index += 1;
                info!("Sfx: grind start");
                let entity = loop_voice(world, &clip, GRIND_GAIN);
                sfx.grinding = Some(entity);
            }
        } else if !grinding {
            stop_voice(world, &mut sfx.grinding);
        }
        // Bail slide loop while wiping out.
        if bailing && !sfx.was_bailing {
            if let Some(clip) = sfx.banks.get(BAIL_BANK).and_then(|bank| bank.first().cloned()) {
                info!("Sfx: bail slide");
                let entity = loop_voice(world, &clip, BAIL_GAIN);
                sfx.bail_sliding = Some(entity);
            }
        } else if !bailing {
            stop_voice(world, &mut sfx.bail_sliding);
        }
        // Landing thud on a new landing, scaled by the fall speed into it.
        if landing_seq != sfx.landing_seq {
            sfx.landing_seq = landing_seq;
            if let Some(clip) = sfx.banks.get(LAND_BANK).and_then(|bank| {
                (!bank.is_empty()).then(|| bank[(sfx.land_index + 1) % bank.len()].clone())
            }) {
                sfx.land_index += 1;
                let impact = (sfx.fall_speed.max(fall) / 8.0).clamp(0.15, 1.0);
                info!("Sfx: landing impact={impact:.2}");
                let entity = one_shot(world, &clip, impact * LAND_GAIN, 0.9 + 0.2 * impact);
                sfx.voices.push((entity, 0.0));
            }
        }
        sfx.was_grinding = grinding;
        sfx.was_bailing = bailing;
        sfx.fall_speed = fall;
        // Ambience bed follows the map; loops until the map changes.
        let bed = bed_for_map(map_path.as_deref(), &sfx.beds);
        if bed.as_ref().map(|(name, _)| name) != Some(&sfx.ambient_bed) {
            stop_voice(world, &mut sfx.ambient);
            sfx.ambient_bed = bed.as_ref().map(|(name, _)| name.clone()).unwrap_or_default();
            if let Some((name, handle)) = bed {
                info!("Sfx: ambience bed {name}");
                let entity = loop_voice(world, &handle, AMBIENT_GAIN);
                sfx.ambient = Some(entity);
            }
        }
        // Retire finished one-shots; voiceless ones time out in a second.
        let dt = world.resource::<Time<Real>>().delta_secs().clamp(0.0, 0.25);
        sfx.voices.retain(|(entity, pending)| {
            if world.get_entity(*entity).is_err() {
                return false;
            }
            if let Some(sink) = world.get::<AudioSink>(*entity) {
                if sink.empty() {
                    world.despawn(*entity);
                    return false;
                }
                return true;
            }
            if pending + dt > 1.0 {
                world.despawn(*entity);
                return false;
            }
            true
        });
        for (_, pending) in &mut sfx.voices {
            *pending += dt;
        }
    });
}
