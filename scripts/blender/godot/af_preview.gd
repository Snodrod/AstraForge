extends Node3D
## AstraForge Forge — FX preview stage for Godot's Movie Maker (godot_package params.fx_preview).
## Frames the packaged asset from a three-quarter view, optionally flies it along its -Z so
## world-space trails stretch behind it, retriggers one-shot FX twice a second, plays autoplay.

@export var speed: float = 0.0          ## asset sizes per second along -Z
@export var elevation_deg: float = 32.0

var _asset: Node3D
var _cam: Camera3D
var _t := 0.0
var _v := 0.0


func _ready() -> void:
	_asset = get_node("asset") as Node3D
	_cam = get_node("cam") as Camera3D
	var aabb := AABB()
	var first := true
	for n in _asset.find_children("*", "MeshInstance3D", true, false):
		var mi := n as MeshInstance3D
		var box: AABB = mi.global_transform * mi.get_aabb()
		aabb = box if first else aabb.merge(box)
		first = false
	var size := maxf(aabb.size.length(), 0.5)
	_v = speed * size
	var c := aabb.get_center()
	var el := deg_to_rad(elevation_deg)
	var dir := Vector3(cos(el) * 0.7071, sin(el), cos(el) * 0.7071)
	_cam.position = c + dir * size * 1.15
	var ahead := size * 0.15 if speed > 0.0 else 0.0
	_cam.look_at(c + Vector3(0, size * 0.05, ahead), Vector3.UP)
	_cam.fov = 45.0
	if _asset.has_method("play_animation") and String(_asset.get("autoplay_animation")) != "":
		_asset.call("play_animation", _asset.get("autoplay_animation"))


func _process(delta: float) -> void:
	_t += delta
	_asset.position.z -= _v * delta
	_cam.position.z -= _v * delta
	if fmod(_t, 0.5) < delta and _asset.has_method("trigger"):
		for fx in _asset.get("attached"):
			if fx is GPUParticles3D and (fx as GPUParticles3D).one_shot:
				_asset.call("trigger", String(fx.name))
