extends SceneTree
## AstraForge Forge — headless validator for packages made by godot_package / render_sprites.
##
##   godot --headless --path <throwaway project> --import
##   godot --headless --path <throwaway project> --script res://validate_package.gd
##
## Reads res://af_check.json:
##   {"scenes": [{"path": "res://packages/x/x.tscn", "sockets": ["socket_a", ...],
##                "fx": [{"socket": "socket_a", "name": "fx_engine_trail",
##                        "expect_axis": [0, 0, 1], "expect_dot": 0.9}],
##                "animations": [{"name": "idle-loop", "loop": true}]}],
##    "fx_scenes": ["res://packages/x/fx/engine_trail.tscn"],
##    "sprite_frames": [{"path": "res://sprites/x/x_frames.tres", "cell": [w, h],
##                       "expect": {"idle_S": {"frames": 12, "loop": true, "speed": 12}}}]}
## Every fx entry reports "axis_y_root": the FX node's +Y (its emit direction) in the package root's space.
## With "expect_axis": [x, y, z] (thruster FX: aft) it also reports "axis_dot" / "axis_ok" and fails the run
## when dot(axis_y_root, expect_axis) < "expect_dot" (default 0.9).
## Prints one line `AFGODOT {json}` and quits with 0 (all ok) or 1.

var cfg: Dictionary = {}
var result: Dictionary = {"ok": true, "errors": []}
var pending: Array = []   # [{spec, inst, report}]
var waited := 0
var finished := false


func _initialize() -> void:
	result["godot"] = String(Engine.get_version_info().get("string", ""))
	if not FileAccess.file_exists("res://af_check.json"):
		_fail("missing res://af_check.json")
		_finish()
		return
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string("res://af_check.json"))
	if typeof(parsed) != TYPE_DICTIONARY:
		_fail("af_check.json is not a JSON object")
		_finish()
		return
	cfg = parsed
	result["sprite_frames"] = []
	for spec in cfg.get("sprite_frames", []):
		result["sprite_frames"].append(_check_sprite_frames(spec))
	result["fx_scenes"] = []
	for path in cfg.get("fx_scenes", []):
		result["fx_scenes"].append(_check_fx_scene(String(path)))
	result["scenes"] = []
	for spec in cfg.get("scenes", []):
		_start_scene(spec)


func _process(_delta: float) -> bool:
	if finished:
		return true
	waited += 1
	if waited < 3:
		return false
	for item in pending:
		_collect_scene(item)
	_finish()
	return true


func _fail(msg: String) -> void:
	result["ok"] = false
	(result["errors"] as Array).append(msg)


func _finish() -> void:
	if finished:
		return
	finished = true
	print("AFGODOT " + JSON.stringify(result))
	quit(0 if result["ok"] else 1)


# ------------------------------------------------------------------ SpriteFrames

func _check_sprite_frames(spec: Dictionary) -> Dictionary:
	var path := String(spec.get("path", ""))
	var rep := {"path": path, "loaded": false, "mismatches": []}
	var res: Resource = ResourceLoader.load(path)
	if res == null or not (res is SpriteFrames):
		_fail("SpriteFrames failed to load: " + path)
		return rep
	var sf := res as SpriteFrames
	rep["loaded"] = true
	var names := sf.get_animation_names()
	rep["animation_count"] = names.size()
	var anims := {}
	for n in names:
		var info := {"frames": sf.get_frame_count(n), "loop": sf.get_animation_loop(n),
			"speed": sf.get_animation_speed(n)}
		if sf.get_frame_count(n) > 0:
			var tex: Texture2D = sf.get_frame_texture(n, 0)
			if tex != null:
				info["frame_size"] = [tex.get_width(), tex.get_height()]
				if tex is AtlasTexture and (tex as AtlasTexture).atlas != null:
					info["atlas_size"] = [(tex as AtlasTexture).atlas.get_width(), (tex as AtlasTexture).atlas.get_height()]
		anims[String(n)] = info
	rep["animations"] = anims
	var expect: Dictionary = spec.get("expect", {})
	var cell: Array = spec.get("cell", [])
	for n in expect:
		var e: Dictionary = expect[n]
		if not anims.has(n):
			(rep["mismatches"] as Array).append("missing animation " + n)
			continue
		var a: Dictionary = anims[n]
		if int(a["frames"]) != int(e.get("frames", a["frames"])):
			(rep["mismatches"] as Array).append("%s frames %d != %d" % [n, a["frames"], e["frames"]])
		if bool(a["loop"]) != bool(e.get("loop", a["loop"])):
			(rep["mismatches"] as Array).append("%s loop %s != %s" % [n, a["loop"], e["loop"]])
		if absf(float(a["speed"]) - float(e.get("speed", a["speed"]))) > 0.01:
			(rep["mismatches"] as Array).append("%s speed %s != %s" % [n, a["speed"], e["speed"]])
		if cell.size() == 2 and a.has("frame_size"):
			if int(a["frame_size"][0]) != int(cell[0]) or int(a["frame_size"][1]) != int(cell[1]):
				(rep["mismatches"] as Array).append("%s frame size %s != %s" % [n, a["frame_size"], cell])
	if not (rep["mismatches"] as Array).is_empty():
		_fail("SpriteFrames mismatches in " + path)
	return rep


# ------------------------------------------------------------------ FX scenes

func _check_fx_scene(path: String) -> Dictionary:
	var rep := {"path": path, "loaded": false}
	var ps: PackedScene = ResourceLoader.load(path) as PackedScene
	if ps == null:
		_fail("FX scene failed to load: " + path)
		return rep
	rep["loaded"] = true
	var node := ps.instantiate()
	rep["class"] = node.get_class()
	if node is GPUParticles3D:
		var gp := node as GPUParticles3D
		rep["amount"] = gp.amount
		rep["lifetime"] = gp.lifetime
		rep["one_shot"] = gp.one_shot
		rep["process_material"] = gp.process_material.get_class() if gp.process_material != null else ""
		rep["draw_pass_1"] = gp.draw_pass_1.get_class() if gp.draw_pass_1 != null else ""
		var mat_ok := false
		if gp.draw_pass_1 is QuadMesh:
			var m: Material = (gp.draw_pass_1 as QuadMesh).material
			if m is StandardMaterial3D:
				mat_ok = (m as StandardMaterial3D).albedo_texture != null
		rep["material_textured"] = mat_ok
		rep["ok"] = gp.process_material is ParticleProcessMaterial and gp.draw_pass_1 is QuadMesh and mat_ok
	else:
		rep["ok"] = false
	if not rep["ok"]:
		_fail("FX scene is not GPUParticles3D + ParticleProcessMaterial + textured QuadMesh: " + path)
	node.free()
	return rep


# ------------------------------------------------------------------ packaged scenes

func _start_scene(spec: Dictionary) -> void:
	var path := String(spec.get("path", ""))
	var rep := {"path": path, "loaded": false, "instanced": false}
	result["scenes"].append(rep)
	var ps: PackedScene = ResourceLoader.load(path) as PackedScene
	if ps == null:
		_fail("scene failed to load: " + path)
		return
	rep["loaded"] = true
	var inst := ps.instantiate()
	if inst == null:
		_fail("scene failed to instantiate: " + path)
		return
	rep["instanced"] = true
	get_root().add_child(inst)
	pending.append({"spec": spec, "inst": inst, "rep": rep})


static func safe_name(n: String) -> String:
	for ch in [":", ".", "@", "/", "\"", "%"]:
		n = n.replace(ch, "_")
	return n


static func loop_base(n: String) -> String:
	for suf in ["-loop", "_loop", "-cycle", "_cycle"]:
		if n.to_lower().ends_with(suf):
			return n.substr(0, n.length() - suf.length())
	return n


func _walk(node: Node, out: Array) -> void:
	out.append(node)
	for c in node.get_children():
		_walk(c, out)


func _collect_scene(item: Dictionary) -> void:
	var spec: Dictionary = item["spec"]
	var inst: Node = item["inst"]
	var rep: Dictionary = item["rep"]
	var nodes: Array = []
	_walk(inst, nodes)
	rep["node_count"] = nodes.size()
	rep["root_class"] = inst.get_class()
	rep["root_name"] = String(inst.name)
	var model := inst.get_node_or_null("model")
	if model is Node3D:
		var b: Basis = (model as Node3D).transform.basis
		rep["model_basis_z"] = [snappedf(b.z.x, 0.001), snappedf(b.z.y, 0.001), snappedf(b.z.z, 0.001)]
	# animations
	var anims := {}
	for n in nodes:
		if n is AnimationPlayer:
			var ap := n as AnimationPlayer
			for a in ap.get_animation_list():
				var an := ap.get_animation(a)
				anims[String(a)] = {"loop_mode": int(an.loop_mode), "length": snappedf(an.length, 0.001),
					"tracks": an.get_track_count()}
	rep["animations"] = anims
	var anim_rep := []
	for e in spec.get("animations", []):
		var want := String(e.get("name", ""))
		var loop := bool(e.get("loop", false))
		var hit := ""
		if anims.has(want):
			hit = want
		elif anims.has(loop_base(want)):
			hit = loop_base(want)
		var r := {"expected": want, "found": hit, "loop_expected": loop}
		if hit == "":
			r["ok"] = false
		else:
			var lm := int(anims[hit]["loop_mode"])
			r["loop_mode"] = lm
			r["ok"] = (lm != 0) == loop
		anim_rep.append(r)
		if not r["ok"]:
			_fail("animation check failed: %s in %s" % [want, spec.get("path", "")])
	rep["animation_checks"] = anim_rep
	# sockets (by glTF extras.game_socket, then by node name; the importer renames ':' and '.')
	var by_socket := {}
	for n in nodes:
		if not (n is Node3D):
			continue
		if n.has_meta("extras"):
			var ex: Variant = n.get_meta("extras")
			if ex is Dictionary and (ex as Dictionary).has("game_socket"):
				by_socket[String((ex as Dictionary)["game_socket"])] = n
		var nm := String(n.name)
		if nm.begins_with("socket_") and not by_socket.has(nm):
			by_socket[nm] = n
	var sock_rep := {}
	for s in spec.get("sockets", []):
		var key := String(s)
		var node: Node3D = by_socket.get(key, by_socket.get(safe_name(key), null))
		if node == null:
			sock_rep[key] = {"found": false}
			_fail("socket not found: " + key)
		else:
			var gt := node.global_transform
			sock_rep[key] = {"found": true, "node": String(node.name), "parent": String(node.get_parent().name),
				"parent_class": node.get_parent().get_class(), "has_extras": node.has_meta("extras"),
				"origin": [snappedf(gt.origin.x, 0.001), snappedf(gt.origin.y, 0.001), snappedf(gt.origin.z, 0.001)],
				"axis_y": [snappedf(gt.basis.y.x, 0.001), snappedf(gt.basis.y.y, 0.001), snappedf(gt.basis.y.z, 0.001)]}
	rep["sockets"] = sock_rep
	rep["sockets_in_scene"] = by_socket.keys()
	# FX attached under sockets
	var fx_rep := []
	for e in spec.get("fx", []):
		var sname := String(e.get("socket", ""))
		var fname := String(e.get("name", ""))
		var sock: Node3D = by_socket.get(sname, by_socket.get(safe_name(sname), null))
		var r := {"socket": sname, "name": fname, "ok": false}
		if sock != null:
			var fx := sock.get_node_or_null(NodePath(fname))
			if fx != null:
				r["class"] = fx.get_class()
				r["ok"] = fx is GPUParticles3D
				if fx is GPUParticles3D:
					var gp := fx as GPUParticles3D
					r["emitting"] = gp.emitting
					# scale of the effect relative to the package root (1 = metres of the asset)
					var rel: Vector3 = ((inst as Node3D).global_transform.affine_inverse() * gp.global_transform).basis.get_scale()
					r["scale_rel"] = snappedf((rel.x + rel.y + rel.z) / 3.0, 0.001)
					# direction the effect emits along (its local +Y) in the package root's space
					var rel_basis: Basis = ((inst as Node3D).global_transform.affine_inverse() * gp.global_transform).basis
					var ay: Vector3 = rel_basis.y.normalized()
					r["axis_y_root"] = [snappedf(ay.x, 0.001), snappedf(ay.y, 0.001), snappedf(ay.z, 0.001)]
					if e.has("expect_axis"):
						var ea: Array = e["expect_axis"]
						var want := Vector3(float(ea[0]), float(ea[1]), float(ea[2])).normalized()
						var dot := ay.dot(want)
						r["axis_dot"] = snappedf(dot, 0.001)
						r["axis_ok"] = dot >= float(e.get("expect_dot", 0.9))
						if not r["axis_ok"]:
							_fail("fx %s at %s emits along %s, expected near %s" % [fname, sname, ay, want])
					var bb := gp.visibility_aabb
					r["visibility_aabb"] = [bb.position.x, bb.position.y, bb.position.z, bb.size.x, bb.size.y, bb.size.z]
					if gp.draw_pass_1 is QuadMesh:
						r["quad_size"] = snappedf((gp.draw_pass_1 as QuadMesh).size.x, 0.0001)
		if not r["ok"]:
			_fail("fx not attached: %s at %s" % [fname, sname])
		fx_rep.append(r)
	rep["fx"] = fx_rep
	var attached: Variant = inst.get("attached")
	rep["script_attached"] = (attached as Array).size() if attached is Array else -1
	var missing: Variant = inst.get("missing_sockets")
	rep["script_missing_sockets"] = Array(missing) if missing is PackedStringArray else []
