extends SceneTree
## AstraForge effect scene check: load() + instantiate() every fx/*.tscn, node and resource types, a few frames in the tree.
var insts: Array = []
var errors: Array = []
var report: Array = []
var frames := 0

func _initialize() -> void:
	var mount = load("res://af_fx_mount.gd")
	if mount == null or not (mount is GDScript):
		errors.append("af_fx_mount.gd did not compile")
	var files := DirAccess.get_files_at("res://fx")
	files.sort()
	for f in files:
		if not f.ends_with(".tscn"):
			continue
		var path := "res://fx/" + f
		var ps = load(path)
		if ps == null or not (ps is PackedScene):
			errors.append("load failed: " + path)
			continue
		var inst: Node = (ps as PackedScene).instantiate()
		if inst == null:
			errors.append("instantiate failed: " + path)
			continue
		var row := {"file": f, "type": inst.get_class(), "name": String(inst.name)}
		if not (inst is GPUParticles3D):
			errors.append(f + ": root is not GPUParticles3D")
		else:
			var p := inst as GPUParticles3D
			row["amount"] = p.amount
			row["lifetime"] = p.lifetime
			row["one_shot"] = p.one_shot
			row["emitting_initial"] = p.emitting
			row["local_coords"] = p.local_coords
			if not (p.process_material is ParticleProcessMaterial):
				errors.append(f + ": process_material is not ParticleProcessMaterial")
			else:
				var pm := p.process_material as ParticleProcessMaterial
				row["emission_shape"] = pm.emission_shape
				row["anim_speed"] = pm.anim_speed_min
				row["turbulence"] = pm.turbulence_enabled
				row["hue"] = pm.hue_variation_max
				row["color_ramp"] = pm.color_ramp != null
				row["scale_curve"] = pm.scale_curve != null
			if not (p.draw_pass_1 is QuadMesh):
				errors.append(f + ": draw_pass_1 is not QuadMesh")
			else:
				var m = (p.draw_pass_1 as QuadMesh).material
				if not (m is StandardMaterial3D):
					errors.append(f + ": quad material is not StandardMaterial3D")
				else:
					var sm := m as StandardMaterial3D
					row["texture"] = sm.albedo_texture != null
					row["texture_size"] = str(sm.albedo_texture.get_size()) if sm.albedo_texture != null else ""
					row["billboard_mode"] = sm.billboard_mode
					row["anim_h"] = sm.particles_anim_h_frames
					row["anim_v"] = sm.particles_anim_v_frames
					row["anim_loop"] = sm.particles_anim_loop
					row["blend"] = sm.blend_mode
					if sm.albedo_texture == null:
						errors.append(f + ": albedo_texture did not load")
			row["lights"] = inst.get_children().filter(func(c): return c is OmniLight3D).size()
			row["meta"] = String(inst.get_meta("astraforge_fx", ""))
		report.append(row)
		get_root().add_child(inst)
		insts.append(inst)

func _process(_delta: float) -> bool:
	frames += 1
	if frames == 6:
		for i in insts:
			if i is GPUParticles3D:
				(i as GPUParticles3D).restart()
	if frames >= 10:
		for i in insts:
			if not is_instance_valid(i):
				errors.append("instance freed unexpectedly")
		print("FXCHECK " + JSON.stringify({"scenes": report.size(), "errors": errors, "report": report, "godot": Engine.get_version_info().get("string", "")}))
		quit(0 if errors.is_empty() else 1)
	return false
