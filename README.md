# TF2 Teleporter Glitch
1. Go to pl_redwood, `r_threaded_particles 1` and `cl_showerror 1`
2. Put `redwood_glitch.nut` in tf/scripts/vscripts/
3. Stand near the water wheel and build a teleporter as engi
4. Run `script_execute redwood_glitch` on the server. Or just blow up some sticky grenades instead of using the script.
5. Stand on top of the teleporter and observe the glitch

https://www.youtube.com/watch?v=I-QNLZllkY4

https://tf2maps.net/threads/preventing-the-falling-through-teleporter-glitch-on-your-map.56659/

## Root Cause
The TF2 engine has a class called `CEngineTraceClient`. This class checks game collisions by doing mathematics.


This class has a field called `m_pRootMoveParent`. This is a pointer to a parent entity's transformation matrix. There is only one instance of this field in the game client engine at once, shared by all traces. (The server behaves differently.)


By default this field is null.


At each trace step, it saves the old value of `m_pRootMoveParent` locally, updates it with the current entity's parent's transformation matrix if applicable, does all the maths to figure out collisions, then restores the previous value of `m_pRootMoveParent`. This is because you can have multiple layers of children that are resolved recursively, so you need to save and restore the parent's parent when recursing up and down those chains. But when the final collision result is calculated, `m_pRootMoveParent` should always return to be "null".


The engine does thousands of these traces every second, but usually always in series, so you'd never have two independent trace chains fighting over the single `m_pRootMoveParent` field.


However, particle effect collision traces are multithreaded by default in the engine (`r_threaded_particles 1`). This means you can have multiple traces overlapping at the same time. This is a race condition.


What can happen is when a particle trace runs concurrent to a normal entity trace, such as explosions happening during the water wheel's rotation, the particle trace incorrectly saves the water wheel's transformation matrix to be restored once the particle trace finishes, instead of null. This incorrect value is then restored permanently into `m_pRootMoveParent` as its new default value.


When you stand on a teleporter, it performs this same game collision trace, but now `m_pRootMoveParent` has been permanently set to the water wheel's transformation matrix, instead of null. The game client then uses this incorrect rotation matrix to calculate your collision with the teleporter, and you fall through, but the server (unaffected by this bug) immediately disagrees and puts you back on top of the teleporter where you should be. This repeats 66 times a second giving the "vibrating" or "rubber banding" effect.


Because the race condition has set a "new start and finish value" for `m_pRootMoveParent` that isn't null, it persists across map changes, and only resets once you restart the game.

## TF2 Entities potentially affected
Most of these are theoretical, but `func_tracktrain`, `func_movelinear` and `func_door_rotating` are confirmed to cause this in `cp_balloon_race_v11`, `pl_blackmoose_b11` and `pl_redwood` (as of 26th September 2026) respectively.

The entities below need something parented to them for the glitch to occur, then translation or rotation of the base entity to happen while collision-enabled particle effects are playing.

| Condition | Root classes |
| --- | --- |
| Always `SOLID_BSP` | `func_button`, `func_breakable`, `func_breakable_surf`, `func_conveyor`, `func_guntarget`, `func_lod`, `func_plat`, `func_platrot`, `func_trackautochange`, `func_trackchange`, `func_train`, `func_vehicleclip`, `func_wall`, `func_wall_toggle` |
| `solidbsp=1` | `func_brush`, `func_door_rotating`, `func_forcefield`, `func_monitor`, `func_reflective_glass`, `func_respawnroomvisualizer`, `func_rotating`, `momentary_rot_button` |
| HL1 Train spawnflag `128` | `func_tracktrain`, `func_tanktrain` |
| TF brush triggers/volumes when unparented | `trigger_brush`, `trigger_soundscape`, `trigger_capture_area`, `trigger_remove`, `trigger_hurt`, `trigger_multiple`, `trigger_once`, `trigger_look`, `trigger_transition`, `trigger_changelevel`, `trigger_push`, `trigger_teleport`, `trigger_teleport_relative`, `trigger_togglesave`, `trigger_autosave`, `trigger_gravity`, `trigger_cdaudio`, `trigger_proximity`, `trigger_impact`, `trigger_playermovement`, `trigger_serverragdoll`, `trigger_apply_impulse`, `trigger_stun`, `trigger_ignite_arrows`, `trigger_timer_door`, `trigger_bot_tag`, `trigger_add_tf_player_condition`, `trigger_player_respawn_override`, `trigger_ignite`, `trigger_particle`, `trigger_remove_tf_player_condition`, `trigger_add_or_remove_tf_player_attributes`, `trigger_passtime_ball`, `trigger_catapult`; also `color_correction_volume`, `func_achievement`, `func_capturezone`, `func_changeclass`, `func_croc`, `func_flag_alert`, `func_flagdetectionzone`, `func_friction`, `func_nav_avoid`, `func_nav_prefer`, `func_nav_prerequisite`, `func_nobuild`, `func_nogrenades`, `func_passtime_goal`, `func_passtime_goalie_zone`, `func_passtime_no_ball_zone`, `func_powerupvolume`, `func_regenerate`, `func_respawnflag`, `func_respawnroom`, `func_suggested_build`, `func_tfbot_hint`, `func_upgradestation` |
| Depends on a BSP ancestor | `func_door` and `func_water` can switch to BSP when spawned under a BSP root; that ancestor is already reported as the root |
