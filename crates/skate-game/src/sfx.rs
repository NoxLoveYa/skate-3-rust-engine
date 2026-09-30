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
const CHEER_BANK: &str = "arena_cheers";
const OHH_BANK: &str = "arena_ohhs";
const UI_POOL: &str = "sk8_menu";
const COLLISION_POOL: &str = "Skate_Collisions";
const UI_GAIN: f32 = 0.35;
const SKID_BANK: &str = "WHEEL_SKID_BANK";
const SEAM_BANK: &str = "Seams_Bank";
const SQUEAK_BANK: &str = "Brd_Squeaks";
const FLIP_BANK: &str = "Sk8_Air_Flip_Tricks";
const WIND_BANK: &str = "sense_of_speed";
const PUSH_BANK: &str = "FOOT_DRAG";
const CROWD_GAIN: f32 = 0.4;
const PUSH_GAIN: f32 = 0.55;
const SKID_GAIN: f32 = 0.55;
const SEAM_GAIN: f32 = 0.3;
const SEAM_METERS: f32 = 4.0;
const SQUEAK_GAIN: f32 = 0.4;
const FLIP_GAIN: f32 = 0.6;
const WIND_GAIN: f32 = 0.35;
/// Second rolling layer. The retail rolling sound is a composite (base wheel
/// loop plus rattles/seams); the banks below come from the engine's own
/// AEMS registry (SK8_AEMS_rolling.csi family).
const RATTLE_BANK: &str = "Rolling_Rattles";

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
    pools: HashMap<String, Vec<Clip>>,
    #[serde(default)]
    ambience: HashMap<String, Clip>,
    #[serde(default)]
    wheels: Vec<Clip>,
}

#[derive(Resource, Default)]
struct Sfx {
    banks: HashMap<String, Vec<Handle<AudioSource>>>,
    pools: HashMap<String, Vec<Handle<AudioSource>>>,
    beds: Vec<(String, Handle<AudioSource>)>,
    wheels: Vec<Handle<AudioSource>>,
    rolling: Option<Entity>,
    ambient: Option<Entity>,
    ambient_bed: String,
    grind_index: usize,
    land_index: usize,
    cheer_index: usize,
    ohh_index: usize,
    flip_index: usize,
    seam_index: usize,
    push_index: usize,
    rattle_index: usize,
    wind_index: usize,
    skid_index: usize,
    brake_index: usize,
    bail_index: usize,
    ui_index: usize,
    grind_t: f32,
    skid_t: f32,
    bail_t: f32,
    brake_t: f32,
    wind_t: f32,
    rattle_t: f32,
    distance: f32,
    was_grounded: bool,
    was_pushing: bool,
    was_bailing: bool,
    menu_row: usize,
    menu_was_open: bool,
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
    for (pool, clips) in &manifest.pools {
        sfx.pools.insert(
            pool.clone(),
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
        assert_eq!(
            (GRIND_BANK, LAND_BANK, BAIL_BANK, CHEER_BANK, OHH_BANK),
            ("GRINDS", "board_scrapes", "Bodyslide", "arena_cheers", "arena_ohhs")
        );
    }
    #[test]
    fn rolling_is_silent_unless_riding() {
        assert_eq!(roll_gain_for(10.0, false), 0.0);
        assert_eq!(roll_gain_for(0.0, true), 0.0);
        assert_eq!(roll_gain_for(0.4, true), 0.0);
        assert!(roll_gain_for(4.0, true) > 0.0);
        assert_eq!(roll_gain_for(100.0, true), ROLL_GAIN);
    }
    #[test]
    fn grind_gain_tracks_slide_speed() {
        assert_eq!(grind_gain_for(0.0), 0.25 * GRIND_GAIN);
        assert_eq!(grind_gain_for(100.0), GRIND_GAIN);
        let mid = grind_gain_for(5.0);
        assert!(mid > grind_gain_for(0.0) && mid < grind_gain_for(100.0));
    }
    #[test]
    fn skid_needs_sideways_slip_at_speed() {
        assert_eq!(slip_cos(0.0, 0.0, 1.0, 0.0), 1.0);
        assert!((slip_cos(5.0, 0.0, 1.0, 0.0) - 1.0).abs() < 0.001);
        assert!(slip_cos(0.0, 5.0, 1.0, 0.0).abs() < 0.001);
        assert_eq!(skid_gain_for(1.0), 0.0);
        assert_eq!(skid_gain_for(0.906), 0.0);
        assert!(skid_gain_for(0.5) > 0.0);
        assert_eq!(skid_gain_for(-1.0), SKID_GAIN);
    }
}

/// Rolling gain from horizontal speed. Anything but riding on the ground
/// (air, grind, bail, off-board, teleport) is silent: a fallen board never
/// keeps blasting the cruise loop.
fn roll_gain_for(speed: f32, riding: bool) -> f32 {
    if !riding {
        return 0.0;
    }
    ((speed - 0.5) / 8.0).clamp(0.0, 1.0) * ROLL_GAIN
}

/// Grind loudness follows slide speed so slow stalls fade against fast rails.
fn grind_gain_for(speed: f32) -> f32 {
    (0.25 + 0.75 * (speed / 10.0).clamp(0.0, 1.0)) * GRIND_GAIN
}

/// Cosine between board travel and board facing. Powerslides break traction
/// past ~25 degrees; the skid loop fades in from there to sideways.
fn slip_cos(vx: f32, vz: f32, fx: f32, fz: f32) -> f32 {
    let denom = (vx * vx + vz * vz).sqrt() * (fx * fx + fz * fz).sqrt();
    if denom <= 0.0 {
        return 1.0;
    }
    ((vx * fx + vz * fz) / denom).clamp(-1.0, 1.0)
}

fn skid_gain_for(cos: f32) -> f32 {
    ((0.906 - cos) / 0.5).clamp(0.0, 1.0) * SKID_GAIN
}

/// Short-bank repeat cadences in seconds. Retail ships these voices as
/// one-shot pools, so sustained states retrigger round-robin clips instead
/// of looping a single short file (which stutters audibly).
const GRIND_REPEAT: f32 = 1.0;
const SKID_REPEAT: f32 = 0.18;
const BRAKE_REPEAT: f32 = 0.12;
const BAIL_REPEAT: f32 = 0.4;
const WIND_REPEAT: f32 = 0.35;
const RATTLE_REPEAT: f32 = 0.9;

/// Deterministic per-trigger pitch wobble so repeats don't sound mechanical.
fn jitter(counter: usize) -> f32 {
    0.95 + 0.01 * ((counter * 37 + 11) % 10) as f32
}

fn direct(world: &mut World) {
    let speed;
    let slip: f32;
    let grinding;
    let bailing;
    let riding;
    let pushing;
    let braking;
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
        // Category 500 is off-board; filtered 1 is plain ground. Rolling
        // needs both: riding the board on the ground, nothing else.
        riding = input.state.category_12 != 500 && input.filtered_state_0 == 1;
        let root = skater.animated_skeleton.roots.animation_to_world;
        slip = slip_cos(raw[0], raw[2], root[2][0], root[2][2]);
        landing_seq = skater.scoring.landing_seq;
        let controls = world.resource::<crate::physics::PlayerControls>();
        pushing = controls.named_intents.contains_key("Pushing");
        braking = controls.named_intents.contains_key("Brake");
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
        let dt = world.resource::<Time<Real>>().delta_secs().clamp(0.0, 0.25);
        // Menu selection ticks from the UI pool.
        let (row, open_now) = match world.get_resource::<crate::graphics_menu::Menu>() {
            Some(menu) => (menu.selected_row(), menu.open),
            None => (0, false),
        };
        if open_now {
            if row != sfx.menu_row || !sfx.menu_was_open {
                if let Some(clip) = sfx.pools.get(UI_POOL).and_then(|pool| {
                    (!pool.is_empty()).then(|| pool[(sfx.ui_index + 1) % pool.len()].clone())
                }) {
                    sfx.ui_index += 1;
                    info!("Sfx: ui click");
                    let entity = one_shot(world, &clip, UI_GAIN, 1.0);
                    sfx.voices.push((entity, 0.0));
                }
            }
            sfx.menu_row = row;
            sfx.menu_was_open = true;
        } else {
            sfx.menu_was_open = false;
        }
        let roll_gain = if menu_open { 0.0 } else { roll_gain_for(speed, riding) };
        if let Some(wheel) = sfx.wheels.last() {
            if sfx.rolling.is_none() {
                let entity = loop_voice(world, wheel, 0.0);
                sfx.rolling = Some(entity);
            }
            if let Some(entity) = sfx.rolling {
                set_gain(world, entity, roll_gain);
            }
        }
        // Rattle layer retriggers over the base wheel loop at speed.
        if riding && !menu_open && speed > 4.0 {
            sfx.rattle_t -= dt;
            if sfx.rattle_t <= 0.0 {
                sfx.rattle_t = RATTLE_REPEAT;
                if let Some(clip) = sfx.banks.get(RATTLE_BANK).and_then(|bank| (!bank.is_empty()).then(|| bank[(sfx.rattle_index + 1) % bank.len()].clone())) {
                    let gain = ((speed - 4.0) / 8.0).clamp(0.0, 1.0) * ROLL_GAIN;
                    let entity = one_shot(world, &clip, gain, jitter(sfx.rattle_index));
                    sfx.voices.push((entity, 0.0));
                }
            }
        } else {
            sfx.rattle_t = 0.0;
        }
        // Powerslide skids retrigger while the board slides sideways.
        let skidding = riding && speed > 3.0 && slip < 0.906;
        if skidding && !menu_open {
            sfx.skid_t -= dt;
            if sfx.skid_t <= 0.0 {
                sfx.skid_t = SKID_REPEAT;
                if let Some(clip) = sfx.banks.get(SKID_BANK).and_then(|bank| (!bank.is_empty()).then(|| bank[(sfx.skid_index + 1) % bank.len()].clone())) {
                    info!("Sfx: powerslide skid");
                    let entity = one_shot(world, &clip, skid_gain_for(slip), jitter(sfx.skid_index));
                    sfx.voices.push((entity, 0.0));
                }
            }
        } else {
            sfx.skid_t = 0.0;
        }
        // Speed wind gusts retrigger at pace, riding or airborne.
        if !menu_open && speed > 8.0 {
            sfx.wind_t -= dt;
            if sfx.wind_t <= 0.0 {
                sfx.wind_t = WIND_REPEAT;
                if let Some(clip) = sfx.banks.get(WIND_BANK).and_then(|bank| (!bank.is_empty()).then(|| bank[(sfx.wind_index + 1) % bank.len()].clone())) {
                    let gain = ((speed - 8.0) / 12.0).clamp(0.0, 1.0) * WIND_GAIN;
                    let entity = one_shot(world, &clip, gain, jitter(sfx.wind_index));
                    sfx.voices.push((entity, 0.0));
                }
            }
        } else {
            sfx.wind_t = 0.0;
        }
        // Push stroke shove on a fresh Pushing intent; foot brake drags
        // the loop while the Brake intent holds.
        if pushing && !sfx.was_pushing {
            if let Some(clip) = sfx.banks.get(PUSH_BANK).and_then(|bank| {
                (!bank.is_empty()).then(|| bank[(sfx.push_index + 1) % bank.len()].clone())
            }) {
                sfx.push_index += 1;
                info!("Sfx: push stroke");
                let entity = one_shot(world, &clip, PUSH_GAIN, 1.0);
                sfx.voices.push((entity, 0.0));
            }
        }
        sfx.was_pushing = pushing;
        if braking && riding && !menu_open {
            sfx.brake_t -= dt;
            if sfx.brake_t <= 0.0 {
                sfx.brake_t = BRAKE_REPEAT;
                if let Some(clip) = sfx.banks.get(PUSH_BANK).and_then(|bank| (!bank.is_empty()).then(|| bank[(sfx.brake_index + 1) % bank.len()].clone())) {
                    info!("Sfx: foot brake");
                    let entity = one_shot(world, &clip, PUSH_GAIN, jitter(sfx.brake_index));
                    sfx.voices.push((entity, 0.0));
                }
            }
        } else {
            sfx.brake_t = 0.0;
        }
        // Takeoff flip snap on leaving the ground with the board.
        let airborne_now = !riding && !grinding && !bailing;
        if airborne_now && sfx.was_grounded {
            if let Some(clip) = sfx.banks.get(FLIP_BANK).and_then(|bank| {
                (!bank.is_empty()).then(|| bank[(sfx.flip_index + 1) % bank.len()].clone())
            }) {
                sfx.flip_index += 1;
                info!("Sfx: takeoff flip");
                let entity = one_shot(world, &clip, FLIP_GAIN, 1.0);
                sfx.voices.push((entity, 0.0));
            }
        }
        sfx.was_grounded = riding;
        // Pavement seam clicks every few meters of riding.
        if riding && !menu_open {
            sfx.distance += speed * dt;
            if sfx.distance >= SEAM_METERS {
                sfx.distance = 0.0;
                if let Some(clip) = sfx.banks.get(SEAM_BANK).and_then(|bank| {
                    (!bank.is_empty()).then(|| bank[(sfx.seam_index + 1) % bank.len()].clone())
                }) {
                    sfx.seam_index += 1;
                    let entity = one_shot(world, &clip, SEAM_GAIN, 1.0);
                    sfx.voices.push((entity, 0.0));
                }
            }
        } else {
            sfx.distance = 0.0;
        }
        // Grind scrapes retrigger while a grind is active.
        if grinding && !menu_open {
            sfx.grind_t -= dt;
            if sfx.grind_t <= 0.0 {
                sfx.grind_t = GRIND_REPEAT;
                if let Some(clip) = sfx.banks.get(GRIND_BANK).and_then(|bank| (!bank.is_empty()).then(|| bank[(sfx.grind_index + 1) % bank.len()].clone())) {
                    info!("Sfx: grind scrape");
                    let entity = one_shot(world, &clip, grind_gain_for(speed), jitter(sfx.grind_index));
                    sfx.voices.push((entity, 0.0));
                }
            }
        } else {
            sfx.grind_t = 0.0;
        }
        // Bail slide rattles while wiping out, plus a crowd groan.
        if bailing && !menu_open {
            sfx.bail_t -= dt;
            if sfx.bail_t <= 0.0 {
                sfx.bail_t = BAIL_REPEAT;
                if let Some(clip) = sfx.banks.get(BAIL_BANK).and_then(|bank| (!bank.is_empty()).then(|| bank[(sfx.bail_index + 1) % bank.len()].clone())) {
                    info!("Sfx: bail slide");
                    let entity = one_shot(world, &clip, BAIL_GAIN, jitter(sfx.bail_index));
                    sfx.voices.push((entity, 0.0));
                }
            }
            if !sfx.was_bailing {
                if let Some(clip) = sfx.banks.get(OHH_BANK).and_then(|bank| (!bank.is_empty()).then(|| bank[(sfx.ohh_index + 1) % bank.len()].clone())) {
                    let entity = one_shot(world, &clip, CROWD_GAIN, 1.0);
                    sfx.voices.push((entity, 0.0));
                }
            }
        } else {
            sfx.bail_t = 0.0;
        }
        sfx.was_bailing = bailing;
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
                // Mid-fall landings creak the trucks on top of the slap.
                if (0.35..0.65).contains(&impact) {
                    if let Some(squeak) = sfx.banks.get(SQUEAK_BANK).and_then(|bank| bank.first().cloned()) {
                        let entity = one_shot(world, &squeak, SQUEAK_GAIN, 1.0);
                        sfx.voices.push((entity, 0.0));
                    }
                }
                // Big air gets a crowd reaction.
                if impact > 0.8 {
                    if let Some(cheer) = sfx.banks.get(CHEER_BANK).and_then(|bank| {
                        (!bank.is_empty()).then(|| bank[(sfx.cheer_index + 1) % bank.len()].clone())
                    }) {
                        sfx.cheer_index += 1;
                        let entity = one_shot(world, &cheer, CROWD_GAIN, 1.0);
                        sfx.voices.push((entity, 0.0));
                    }
                }
                // Harsh impacts layer a collision pool hit.
                if impact > 0.9 {
                    if let Some(hit) = sfx.pools.get(COLLISION_POOL).and_then(|pool| {
                        (!pool.is_empty()).then(|| pool[(sfx.land_index + 1) % pool.len()].clone())
                    }) {
                        let entity = one_shot(world, &hit, LAND_GAIN, 1.0);
                        sfx.voices.push((entity, 0.0));
                    }
                }
            }
        }
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
