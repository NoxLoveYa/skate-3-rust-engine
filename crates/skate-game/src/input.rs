//! Platform input adapter; no animation or physics state mutation here.
use crate::app::SimulationSet;
use bevy::prelude::*;

mod controllers;
pub(crate) mod gesture_catalog;
mod gesture_mapping_data;
pub(crate) mod gesture_mapping;
pub(crate) mod gesture_input;
mod keyboard;
pub(crate) mod platform;
pub(crate) use controllers::{ControllerInput, ControllerStatus};
use skate_core::input::tick::TickInput;

#[derive(Resource, Clone, Copy, Debug)]
pub(crate) struct PublishedTickInput(pub TickInput);

impl Default for PublishedTickInput {
    fn default() -> Self {
        Self(TickInput::new(
            0,
            skate_core::input::gameplay_map::GameplayActions::from_values([0.0; 18]),
            false,
        ))
    }
}

pub(crate) struct InputPlugin;
impl Plugin for InputPlugin {
    fn build(&self, app: &mut App) {
        app.init_resource::<ControllerInput>()
            .init_resource::<PublishedTickInput>()
            .add_systems(PreUpdate, poll_controllers.run_if(crate::graphics_menu::gameplay_active))
            .add_systems(FixedUpdate, publish_actions.in_set(SimulationSet::Input));
    }
}

pub(crate) fn poll_controllers(mut input: ResMut<ControllerInput>,config:Res<crate::config::Config>,net:Option<Res<crate::multiplayer::Multiplayer>>,windows:Query<&Window>,keys:Res<ButtonInput<KeyCode>>,mut capabilities:Local<[platform::CapabilityCache;4]>,mut keyboard:Local<keyboard::KeyboardState>) {
    let previous = input.status;
    let focused=windows.iter().any(|w|w.focused);
    let active=net.is_some_and(|n|n.active());
    let owned = |slot: usize| !active || !((!focused && config.multiplayer.controller.is_none()) || config.multiplayer.controller.is_some_and(|selected|selected as usize!=slot));
    let mut samples = std::array::from_fn(|slot| {
        if !owned(slot) {
            capabilities[slot].invalidate();
            Err(platform::DeviceError::Disconnected)
        } else {platform::poll_cached(slot, &mut capabilities[slot])}
    });
    // Padless fallback: synthesize slot 0 from the keyboard when the slot is
    // locally owned but has no platform pad. Real pads always win; remote
    // net-filtered slots are untouched.
    if matches!(samples[0], Err(platform::DeviceError::Disconnected)) && owned(0) {
        if let Some(packet) = keyboard::sample(&keys, &mut keyboard) { samples[0] = Ok(packet); }
    }
    input.collect(samples);
    for (index, (&before, &after)) in previous.iter().zip(&input.status).enumerate() {
        if before != after {
            match after {
                ControllerStatus::Ready => info!("Controller {index}: raw platform pad ready"),
                ControllerStatus::Unavailable(platform::DeviceError::Disconnected) => {
                    info!("Controller {index}: disconnected");
                }
                _ => warn!("Controller {index}: {after:?}"),
            }
        }
    }
}

fn publish_actions(
    mut input: ResMut<ControllerInput>,
    mut published: ResMut<PublishedTickInput>,
    menu:Option<Res<crate::graphics_menu::Menu>>,
) {
    if !crate::graphics_menu::gameplay_active(menu) {input.discard_gameplay();}
    input.publish_actions();
    published.0 = input.tick_input();
}

#[cfg(test)]
pub(crate) mod manual_replay;
