# Rigging and animation for Godot

Open `http://127.0.0.1:3210` and go to the **Bones** tab. All operations of this editor run locally in the browser; SkinTokens and UniMate are not needed for them.

## Character → skeleton → clips

1. Import a prepared GLB or add a T-pose mannequin. Select the model's root object.
2. Choose a skeleton template: Humanoid, Quadruped, Mechanical or Custom. Click "Create skeleton".
3. In **Rest**, select bones and fit the joints to the anatomy. W moves, E rotates; the properties on the right set exact values. The position of a child bone defines the segment between joints. Before binding you can add child bones and delete branches.
4. Click "Bind to bones". Smooth mode creates initial weights from the distance to segments with a maximum of four influences. Rigid mode assigns each separate mesh a single nearest bone and suits mechanisms made of separate parts.
5. In **Pose**, check the shoulders, elbows, knees and head. Template fitting and normalized weights do not yet prove correct anatomical deformation.
6. Select a bone and enable weight viewing. Blue is zero influence, red is full. In brush mode the left mouse button changes surface weights; the other influences are normalized automatically. The right mouse button and wheel move the camera. Ctrl+Z undoes a stroke.
7. Create a new clip, for example `Attack`. At time 0 add a key for the selected bone, then set the time, change its pose and add the next key. Keys of different bones go into a shared clip.
8. Save the clip to the library. You can have `Idle`, `Walk`, `Run`, `Attack` and other named motions. The Idle/Walk/Run buttons create only simple procedural in-place starting points for humanoids. For quadrupeds and unusual creatures, author the motion by hand.
9. Check the weights and export **GLB: skeleton, weights and clips**. The export is made from the rest pose and does not include bone markers or colour debugging. For a single name, the current clip replaces the saved variant during export.
10. Save the project to continue working with bones, weights and clips after a restart. If you change the rest pose after creating animations, re-check the clips: full retargeting is not implemented here yet.

## Import into Godot

Copy the GLB into the project folder and wait for the import. The scene will contain a Skeleton3D, a MeshInstance3D with a Skin and an AnimationPlayer with the named clips.

To transfer human motions between different skeletons, configure **BoneMap / SkeletonProfileHumanoid** in Advanced Import Settings. Godot takes rest transforms into account, not just name matches. For root motion, author the Root motion and separately choose the corresponding track in AnimationTree; the procedural starting clips of this version are in-place. [Godot documentation](https://docs.godotengine.org/en/stable/tutorials/assets_pipeline/retargeting_3d_skeletons.html).

Changing the pose in the editor does not automatically create a clip: add keyframes and save the motion. Object and bone keys are merged into the current animation.

## What was verified

Import and playback were verified in official **Godot 4.7.2** with a test mannequin created in AstraForge (a skeleton, brush-corrected weights and the clips `Idle`, `Walk`, `ArmRaise`): 1 Skeleton3D, 27 bones, 15 skinned meshes. Walk changes the pose of LeftUpperLeg, the manual ArmRaise changes the pose of LeftUpperArm. The Godot import was done headless; this verifies structure and animation poses, not an audit of all visual deformation or a game controller.

Also verified: saving and restoring skin attributes via JSON, brush normalization, and a GLB with inverse bind matrices. Neural auto-riggers, IK, retargeting of foreign motions, automatic simplification, fingers/face and anatomy-based weight adaptation remain for later stages.
