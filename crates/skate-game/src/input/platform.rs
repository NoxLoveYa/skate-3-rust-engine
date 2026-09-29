//! Device transport. Raw signed axes/trigger bytes reach the TU3
//! converter without Bevy/gilrs deadzones or normalized-axis reconstruction.
//! Windows uses XInput directly; macOS/Linux use gilrs (same backend Bevy
//! uses) polled synchronously so the native Pad edge/repeat behavior is
//! preserved on every host frame.
use skate_core::input::xbox::XboxState;

pub(crate) struct DevicePacket {
    pub number: u32,
    pub state: XboxState,
    pub subtype: u8,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum DeviceError {
    Disconnected,
    State(u32),
    Capabilities(u32),
    #[cfg(not(windows))]
    UnsupportedPlatform,
}

/// Device identity is metadata; raw input is still sampled every host frame.
/// Refresh periodically as well as after errors, so hot swaps cannot leave a
/// subtype cached indefinitely even if Windows never exposes a disconnect.
#[derive(Default)]
pub(crate) struct CapabilityCache {
    value: Option<(u8, std::time::Instant)>,
}
impl CapabilityCache {
    pub(crate) fn invalidate(&mut self) {
        self.value = None;
    }
    fn get(
        &mut self,
        now: std::time::Instant,
        read: impl FnOnce() -> Result<u8, DeviceError>,
    ) -> Result<u8, DeviceError> {
        if let Some((subtype, expires)) = self.value {
            if now < expires {
                return Ok(subtype);
            }
        }
        self.value = None;
        let subtype = read()?;
        self.value = Some((subtype, now + std::time::Duration::from_secs(1)));
        Ok(subtype)
    }
}

#[cfg(windows)]
mod windows {
    use super::*;
    use std::mem::MaybeUninit;

    // ABI from the installed Windows SDK Xinput.h. No OS-owned pointers are
    // retained and only successful calls permit reading output storage.
    #[repr(C)]
    struct Gamepad {
        buttons: u16,
        left_trigger: u8,
        right_trigger: u8,
        left_x: i16,
        left_y: i16,
        right_x: i16,
        right_y: i16,
    }
    #[repr(C)]
    struct State {
        number: u32,
        gamepad: Gamepad,
    }
    #[repr(C)]
    struct Vibration {
        left: u16,
        right: u16,
    }
    #[repr(C)]
    struct Capabilities {
        device_type: u8,
        subtype: u8,
        flags: u16,
        gamepad: Gamepad,
        vibration: Vibration,
    }
    const _: () = assert!(size_of::<Gamepad>() == 12);
    const _: () = assert!(size_of::<State>() == 16);
    const _: () = assert!(size_of::<Capabilities>() == 20);

    #[link(name = "xinput")]
    unsafe extern "system" {
        fn XInputGetState(index: u32, state: *mut State) -> u32;
        fn XInputGetCapabilities(index: u32, flags: u32, capabilities: *mut Capabilities) -> u32;
    }

    pub(super) fn poll(
        index: u32,
        cache: &mut CapabilityCache,
    ) -> Result<DevicePacket, DeviceError> {
        let mut state = MaybeUninit::<State>::uninit();
        // SAFETY: properly aligned writable storage with the SDK's exact C ABI.
        let result = unsafe { XInputGetState(index, state.as_mut_ptr()) };
        if result != 0 {
            cache.invalidate();
        }
        if result == 1167 {
            return Err(DeviceError::Disconnected);
        }
        if result != 0 {
            return Err(DeviceError::State(result));
        }
        let subtype = cache.get(std::time::Instant::now(), || {
            let mut capabilities = MaybeUninit::<Capabilities>::uninit();
            // SAFETY: writable storage with the SDK ABI; read only on success.
            let result = unsafe { XInputGetCapabilities(index, 1, capabilities.as_mut_ptr()) };
            if result != 0 {
                return Err(DeviceError::Capabilities(result));
            }
            Ok(unsafe { capabilities.assume_init() }.subtype)
        })?;
        // SAFETY: successful XInputGetState initialized the complete structure.
        let state = unsafe { state.assume_init() };
        Ok(DevicePacket {
            number: state.number,
            state: XboxState {
                buttons: state.gamepad.buttons,
                triggers: [state.gamepad.left_trigger, state.gamepad.right_trigger],
                left: [state.gamepad.left_x, state.gamepad.left_y],
                right: [state.gamepad.right_x, state.gamepad.right_y],
            },
            subtype,
        })
    }
}

pub(crate) fn poll_cached(
    index: usize,
    cache: &mut CapabilityCache,
) -> Result<DevicePacket, DeviceError> {
    assert!(index < 4);
    #[cfg(windows)]
    return windows::poll(index as u32, cache);
    #[cfg(not(windows))]
    return desktop::poll(index, cache);
}

/// gilrs-backed transport for macOS/Linux. Maps the standard gilrs layout to
/// the XInput ABI the TU3 converter expects, without deadzones or curve
/// reconstruction: raw -1..1 sticks scale linearly to i16, 0..1 triggers to
/// u8, buttons to XInput bit positions. Packet numbers synthesize the XInput
/// behavior of incrementing only when the raw bytes change.
#[cfg(not(windows))]
mod desktop {
    use super::*;
    use std::sync::{Mutex, OnceLock};

    // XInput button bits (Xinput.h).
    const DPAD_UP: u16 = 0x0001;
    const DPAD_DOWN: u16 = 0x0002;
    const DPAD_LEFT: u16 = 0x0004;
    const DPAD_RIGHT: u16 = 0x0008;
    const START: u16 = 0x0010;
    const BACK: u16 = 0x0020;
    const LEFT_THUMB: u16 = 0x0040;
    const RIGHT_THUMB: u16 = 0x0080;
    const LEFT_SHOULDER: u16 = 0x0100;
    const RIGHT_SHOULDER: u16 = 0x0200;
    const BTN_A: u16 = 0x1000;
    const BTN_B: u16 = 0x2000;
    const BTN_X: u16 = 0x4000;
    const BTN_Y: u16 = 0x8000;

    struct Shared {
        gilrs: gilrs::Gilrs,
        last: [(u16, u8, u8, i16, i16, i16, i16); 4],
        packet: [u32; 4],
    }

    fn shared() -> Result<std::sync::MutexGuard<'static, Option<Shared>>, DeviceError> {
        static CELL: OnceLock<Mutex<Option<Shared>>> = OnceLock::new();
        let cell = CELL.get_or_init(|| {
            Mutex::new(gilrs::Gilrs::new().ok().map(|gilrs| Shared {
                gilrs,
                last: [(0, 0, 0, 0, 0, 0, 0); 4],
                packet: [0; 4],
            }))
        });
        let guard = cell.lock().map_err(|_| DeviceError::State(1))?;
        if guard.is_none() {
            return Err(DeviceError::UnsupportedPlatform);
        }
        Ok(guard)
    }

    fn axis_i16(value: f32) -> i16 {
        (value.clamp(-1.0, 1.0) * 32767.0).round() as i16
    }

    fn trigger_u8(value: f32) -> u8 {
        (value.clamp(0.0, 1.0) * 255.0).round() as u8
    }

    pub(super) fn poll(index: usize, _cache: &mut CapabilityCache) -> Result<DevicePacket, DeviceError> {
        // Sample the pad inside a scope so the gilrs borrow ends before we
        // mutate packet counters below.
        let current: (u16, u8, u8, i16, i16, i16, i16) = {
            let mut guard = shared()?;
            let shared = guard.as_mut().ok_or(DeviceError::UnsupportedPlatform)?;
            while shared.gilrs.next_event().is_some() {}
            // gilrs IDs are opaque handles, not 0..n slots. Map our 4 TU3 device
            // slots to the nth currently connected gamepad in enumeration order.
            let id = shared.gilrs.gamepads().map(|(id, _)| id).nth(index).ok_or(DeviceError::Disconnected)?;
            let pad = shared.gilrs.gamepad(id);
            if !pad.is_connected() {
                return Err(DeviceError::Disconnected);
            }
            // Use the high-level Gamepad API (Button/Axis mapping). The raw
            // GamepadState API takes low-level Codes instead.
            let mut buttons: u16 = 0;
            if pad.is_pressed(gilrs::Button::DPadUp) { buttons |= DPAD_UP; }
            if pad.is_pressed(gilrs::Button::DPadDown) { buttons |= DPAD_DOWN; }
            if pad.is_pressed(gilrs::Button::DPadLeft) { buttons |= DPAD_LEFT; }
            if pad.is_pressed(gilrs::Button::DPadRight) { buttons |= DPAD_RIGHT; }
            if pad.is_pressed(gilrs::Button::Start) { buttons |= START; }
            if pad.is_pressed(gilrs::Button::Select) { buttons |= BACK; }
            if pad.is_pressed(gilrs::Button::LeftThumb) { buttons |= LEFT_THUMB; }
            if pad.is_pressed(gilrs::Button::RightThumb) { buttons |= RIGHT_THUMB; }
            if pad.is_pressed(gilrs::Button::LeftTrigger) { buttons |= LEFT_SHOULDER; }
            if pad.is_pressed(gilrs::Button::RightTrigger) { buttons |= RIGHT_SHOULDER; }
            if pad.is_pressed(gilrs::Button::South) { buttons |= BTN_A; }
            if pad.is_pressed(gilrs::Button::East) { buttons |= BTN_B; }
            if pad.is_pressed(gilrs::Button::West) { buttons |= BTN_X; }
            if pad.is_pressed(gilrs::Button::North) { buttons |= BTN_Y; }
            // Prefer analog trigger axes when the backend exposes them; fall back
            // to digital shoulder buttons so 2-position pads still kickflip.
            // gilrs exposes triggers as LeftZ/RightZ axes (0..1) and as
            // LeftTrigger2/RightTrigger2 buttons; axes give finer granularity.
            let lt_raw = pad.value(gilrs::Axis::LeftZ);
            let rt_raw = pad.value(gilrs::Axis::RightZ);
            let lt = if lt_raw > 0.0005 { trigger_u8(lt_raw) } else if buttons & LEFT_SHOULDER != 0 { 255 } else { 0 };
            let rt = if rt_raw > 0.0005 { trigger_u8(rt_raw) } else if buttons & RIGHT_SHOULDER != 0 { 255 } else { 0 };
            let lx = axis_i16(pad.value(gilrs::Axis::LeftStickX));
            let ly = axis_i16(pad.value(gilrs::Axis::LeftStickY));
            let rx = axis_i16(pad.value(gilrs::Axis::RightStickX));
            let ry = axis_i16(pad.value(gilrs::Axis::RightStickY));
            (buttons, lt, rt, lx, ly, rx, ry)
        };
        // Synthesize XInput packet numbers: bump only when raw bytes change.
        let mut guard = shared()?;
        let shared = guard.as_mut().ok_or(DeviceError::UnsupportedPlatform)?;
        if current != shared.last[index] {
            shared.last[index] = current;
            shared.packet[index] = shared.packet[index].wrapping_add(1);
        }
        let (buttons, lt, rt, lx, ly, rx, ry) = current;
        Ok(DevicePacket {
            number: shared.packet[index],
            state: XboxState { buttons, triggers: [lt, rt], left: [lx, ly], right: [rx, ry] },
            // Standard gamepad; only subtype 7 zeroes TU3 byte 13.
            subtype: 1,
        })
    }
}

#[cfg(test)]
mod cache_tests {
    use super::*;
    #[test]
    fn capability_cache_refreshes_and_never_caches_errors() {
        let start = std::time::Instant::now();
        let mut cache = CapabilityCache::default();
        assert_eq!(cache.get(start, || Ok(1)), Ok(1));
        assert_eq!(
            cache.get(start + std::time::Duration::from_millis(999), || panic!(
                "redundant capability query"
            )),
            Ok(1)
        );
        assert_eq!(
            cache.get(start + std::time::Duration::from_secs(1), || Ok(2)),
            Ok(2)
        );
        cache.invalidate();
        assert_eq!(
            cache.get(start, || Err(DeviceError::Capabilities(5))),
            Err(DeviceError::Capabilities(5))
        );
        assert_eq!(cache.get(start, || Ok(3)), Ok(3));
        cache.invalidate();
        assert_eq!(cache.get(start, || Ok(4)), Ok(4));
    }
}

// Preserve the uncached API for menu-only polling.
pub(crate) fn poll(index: usize) -> Result<DevicePacket, DeviceError> {
    poll_cached(index, &mut CapabilityCache::default())
}
