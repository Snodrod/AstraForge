extends Node3D
## AstraForge Forge — FX mount for a packaged asset (copied next to <asset_id>.tscn by godot_package).
##
## On _ready finds the socket nodes inside the instanced model and instantiates the FX scenes there.
## Sockets are looked up by glTF extras `game_socket` first, then by node name; Godot's importer
## renames ':' '.' '@' '/' '"' '%' to '_', so both spellings are tried. FX emit along their own local +Y,
## which for most sockets is the socket's local +Y (= the Blender socket's local +Z: outward normal / blade
## direction). A binding may carry "emit_axis" (Vector3, in the socket's axes): the FX node is then turned so
## that its +Y lies on that axis - godot_package sets it on thruster sockets, whose exhaust axis depends on the
## ship's extras.frame (thrust: +Z, exhaust: -Z) or, without one, on the geometry.
## No class_name on purpose: several packages may live in one project.

## Node that instances the GLB (child of this root).
@export var model_path: NodePath = ^"model"
## [{"socket": "socket_thruster_main_l", "scene": PackedScene, "name": "fx_engine_trail",
##   "emit_axis": Vector3(0, 0, 1)}]   (emit_axis optional: socket-local direction the FX +Y is turned onto)
@export var fx_bindings: Array = []
## Animation to play on _ready ("" = none). Matches the imported name ("idle") or the source ("idle-loop").
@export var autoplay_animation: String = ""

var attached: Array = []
var missing_sockets: PackedStringArray = PackedStringArray()
var _sockets: Dictionary = {}


func _ready() -> void:
	var model := get_node_or_null(model_path)
	if model == null:
		push_warning("AstraForge fx mount: model node '%s' not found" % model_path)
		return
	_index(model)
	for b in fx_bindings:
		if typeof(b) != TYPE_DICTIONARY:
			continue
		var sock_name := String(b.get("socket", ""))
		var scene: PackedScene = b.get("scene") as PackedScene
		var sock := find_socket(sock_name)
		if sock == null or scene == null:
			missing_sockets.append(sock_name)
			push_warning("AstraForge fx mount: socket '%s' not found or FX scene missing" % sock_name)
			continue
		var fx := scene.instantiate()
		fx.name = String(b.get("name", "fx"))
		sock.add_child(fx)
		var axis: Variant = b.get("emit_axis")
		if axis is Vector3 and fx is Node3D:
			(fx as Node3D).basis = axis_basis(axis as Vector3)
		_keep_metric(fx, sock)
		attached.append(fx)
	if autoplay_animation != "":
		play_animation(autoplay_animation)


## Rotation that takes +Y onto `axis` (any length): identity for +Y, a half turn about X for -Y.
## Node3D.scale keeps this rotation, so _keep_metric can still undo a scaled socket afterwards.
static func axis_basis(axis: Vector3) -> Basis:
	if axis.length() < 0.000001:
		return Basis.IDENTITY
	var a := axis.normalized()
	if a.dot(Vector3.UP) < -0.9999:
		return Basis(Vector3.RIGHT, PI)
	return Basis(Quaternion(Vector3.UP, a))


## FX sizes are metres of the package (godot_package scales presets to the asset). A socket that sits
## under a scaled node inside the GLB (e.g. a 0.01 armature from a cm FBX) would shrink or blow up the
## effect, so undo the socket's scale relative to this root. Scaling the package root itself (e.g. a
## game's fit-to-size) still scales the FX together with the model.
func _keep_metric(fx: Node, sock: Node3D) -> void:
	if not (fx is Node3D):
		return
	var rel: Vector3 = (global_transform.affine_inverse() * sock.global_transform).basis.get_scale()
	var s := (absf(rel.x) + absf(rel.y) + absf(rel.z)) / 3.0
	if s > 0.000001 and absf(s - 1.0) > 0.01:
		(fx as Node3D).scale = Vector3.ONE / s


static func godot_name(n: String) -> String:
	for ch in [":", ".", "@", "/", "\"", "%"]:
		n = n.replace(ch, "_")
	return n


func _index(node: Node) -> void:
	if node is Node3D:
		if node.has_meta("extras"):
			var ex: Variant = node.get_meta("extras")
			if ex is Dictionary and (ex as Dictionary).has("game_socket"):
				_sockets[String((ex as Dictionary)["game_socket"])] = node
		var nm := String(node.name)
		if not _sockets.has(nm):
			_sockets[nm] = node
	for c in node.get_children():
		_index(c)


## Socket node by contract name (or by its Godot-sanitised spelling), null when absent.
func find_socket(socket_name: String) -> Node3D:
	if _sockets.has(socket_name):
		return _sockets[socket_name] as Node3D
	var alt := godot_name(socket_name)
	if _sockets.has(alt):
		return _sockets[alt] as Node3D
	return null


## Restart one-shot emitters (muzzle_flash, magic bursts) under a socket or with a given FX name.
func trigger(socket_or_fx: String) -> void:
	for fx in attached:
		if not is_instance_valid(fx):
			continue
		if fx.name == socket_or_fx or fx.get_parent().name == socket_or_fx \
				or fx.get_parent().name == godot_name(socket_or_fx):
			if fx is GPUParticles3D:
				(fx as GPUParticles3D).restart()
				(fx as GPUParticles3D).emitting = true


func play_animation(anim: String) -> bool:
	# Godot's importer strips '-loop'/'_loop'/'-cycle'/'_cycle' (the GLB says 'idle-loop', the player 'idle';
	# a packaged 'pitch_cycle-cycle' becomes 'pitch_cycle'): try the name as given, then without one suffix.
	var cands: Array[String] = [anim]
	for suf in ["-loop", "_loop", "-cycle", "_cycle"]:
		if anim.ends_with(suf):
			cands.append(anim.trim_suffix(suf))
	var players := find_children("*", "AnimationPlayer", true, false)
	for p in players:
		var ap := p as AnimationPlayer
		for cand in cands:
			if ap.has_animation(cand):
				ap.play(cand)
				return true
	return false
