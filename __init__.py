bl_info = {
    "name": "Vertex Color - Edit Mode",
    "author": "You",
    "version": (2, 0, 0),
    "blender": (4, 3, 0),
    "location": "3D Viewport > N-Panel > Vertex Color (Edit Mode)",
    "description": "Paint and adjust vertex colors directly in Edit Mode",
    "category": "3D View",
}

import bpy
import bmesh
import random
import colorsys


# -----------------------------------------------------------------------------
# Color space helpers (byte layers store sRGB, the picker works in linear)
# -----------------------------------------------------------------------------

def _linear_to_srgb(c):
    return c / 12.92 if c <= 0.0031308 else 1.055 * (c ** (1.0 / 2.4)) - 0.055


def _srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _to_layer_space(color, layer_is_byte):
    if layer_is_byte:
        return (*(_linear_to_srgb(c) for c in color[:3]), color[3])
    return color


def _from_layer_space(color, layer_is_byte):
    if layer_is_byte:
        return (*(_srgb_to_linear(c) for c in color[:3]), color[3])
    return color


# -----------------------------------------------------------------------------
# Shared helpers
# -----------------------------------------------------------------------------

def _loop_layer_collection(bm, data_type):
    """Byte colors and float colors live in separate bmesh layer collections."""
    if data_type == 'FLOAT_COLOR':
        coll = getattr(bm.loops.layers, 'float_color', None)
        if coll is not None:
            return coll
    return bm.loops.layers.color


def _find_loop_layer(bm, name, data_type=None):
    """Look up a loop color layer by name across both collections, creating
    it (with the given data type) when it doesn't exist yet."""
    layer = bm.loops.layers.color.get(name)
    if layer is None:
        coll = getattr(bm.loops.layers, 'float_color', None)
        if coll is not None:
            layer = coll.get(name)
    if layer is None and data_type is not None:
        layer = _loop_layer_collection(bm, data_type).new(name)
    return layer


def _get_context(context):
    """Return (obj, bm, layer, is_byte) or (None, None, None, None) on failure."""
    obj = context.active_object
    if not obj or obj.type != 'MESH' or obj.mode != 'EDIT':
        return None, None, None, None
    attr = obj.data.color_attributes.active
    if attr and attr.domain == 'POINT':
        return obj, None, None, None  # caller reports the domain problem
    bm = bmesh.from_edit_mesh(obj.data)
    if attr is None:
        layer = bm.loops.layers.color.new("Color")
        is_byte = True
    else:
        layer = _find_loop_layer(bm, attr.name, attr.data_type)
        is_byte = attr.data_type == 'BYTE_COLOR'
    return obj, bm, layer, is_byte



def _average_color(loops, layer):
    avg = [0.0, 0.0, 0.0, 0.0]
    for loop in loops:
        c = loop[layer]
        for i in range(4):
            avg[i] += c[i]
    n = len(loops)
    return tuple(v / n for v in avg)


def _check_domain(operator, obj, bm):
    if bm is None and obj is not None:
        operator.report({'ERROR'},
                        "Active color attribute uses the Vertex domain. "
                        "Switch it to Face Corner to use this tool.")
        return True
    if obj is None:
        operator.report({'ERROR'}, "Need a mesh in Edit Mode")
        return True
    return False


# -----------------------------------------------------------------------------
# Properties
# -----------------------------------------------------------------------------

class VCEM_Properties(bpy.types.PropertyGroup):
    color: bpy.props.FloatVectorProperty(
        name="Color",
        subtype='COLOR',
        size=4,
        min=0.0, max=1.0,
        default=(1.0, 1.0, 1.0, 1.0),
    )
    split_faces: bpy.props.BoolProperty(
        name="Split Faces (Hard Edges)",
        description="ON: paint each face's corners individually for hard color borders. "
                    "OFF: flood the color onto shared vertices so colors blend smoothly across faces",
        default=True,
    )


# -----------------------------------------------------------------------------
# Operators
# -----------------------------------------------------------------------------

def _target_loop_list(bm, targets):
    """Flatten [(face_index, [loop_positions])] into a flat BMLoop list."""
    loops_out = []
    for face_index, positions in targets:
        face_loops = list(bm.faces[face_index].loops)
        loops_out.extend(face_loops[p] for p in positions)
    return loops_out


class MESH_OT_VCEM_ApplyColor(bpy.types.Operator):
    """Apply the chosen source color to the selection (faces, or the loops of
    selected verts/edges)"""
    bl_idname = "mesh.vcem_apply_color"
    bl_label = "Apply Color"
    bl_options = {'REGISTER', 'UNDO'}

    source: bpy.props.EnumProperty(
        items=[
            ('WHEEL', "Color Wheel", "Apply the color from the wheel"),
            ('ACTIVE', "Active Face", "Apply the color of the active face (wheel is ignored)"),
            ('AVERAGE', "Average of Selection", "Apply the average color of the selection (wheel is ignored)"),
        ],
        default='WHEEL',
    )

    @classmethod
    def poll(cls, context):
        return (context.active_object and context.active_object.type == 'MESH'
                and context.active_object.mode == 'EDIT')

    def execute(self, context):
        props = context.scene.vcem
        obj, bm, layer, is_byte = _get_context(context)
        if _check_domain(self, obj, bm):
            return {'CANCELLED'}

        bm.faces.ensure_lookup_table()
        targets = _collect_target_loops(bm, context)
        if not targets:
            self.report({'ERROR'}, "No elements selected")
            return {'CANCELLED'}
        target_loops = _target_loop_list(bm, targets)

        if self.source == 'WHEEL':
            color = props.color[:]
        elif self.source == 'ACTIVE':
            if not bm.faces.active or not bm.faces.active.select:
                self.report({'ERROR'}, "No active face selected")
                return {'CANCELLED'}
            color = _from_layer_space(
                _average_color(list(bm.faces.active.loops), layer), is_byte)
        else:  # AVERAGE
            color = _from_layer_space(
                _average_color(target_loops, layer), is_byte)

        color = _to_layer_space(color, is_byte)

        if props.split_faces:
            for loop in target_loops:
                loop[layer] = color
        else:
            # Flood shared vertices so colors blend smoothly across faces
            done = set()
            for loop in target_loops:
                v = loop.vert
                if v.index in done:
                    continue
                done.add(v.index)
                for shared in v.link_loops:
                    shared[layer] = color

        bmesh.update_edit_mesh(obj.data)
        return {'FINISHED'}


class MESH_OT_VCEM_RandomizeWheel(bpy.types.Operator):
    """Generate a random pleasant color and load it into the color wheel"""
    bl_idname = "mesh.vcem_randomize_wheel"
    bl_label = "Random Color"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.vcem
        rgb = colorsys.hsv_to_rgb(random.random(),
                                  random.uniform(0.5, 1.0),
                                  random.uniform(0.7, 1.0))
        props.color = (*rgb, 1.0)
        return {'FINISHED'}


class MESH_OT_VCEM_SampleWheel(bpy.types.Operator):
    """Load a color from the mesh into the color wheel"""
    bl_idname = "mesh.vcem_sample_wheel"
    bl_label = "Sample to Wheel"
    bl_options = {'REGISTER', 'UNDO'}

    source: bpy.props.EnumProperty(
        items=[
            ('ACTIVE', "Active Face", ""),
            ('AVERAGE', "Average of Selection", ""),
        ],
        default='ACTIVE',
    )

    @classmethod
    def poll(cls, context):
        return (context.active_object and context.active_object.type == 'MESH'
                and context.active_object.mode == 'EDIT')

    def execute(self, context):
        obj, bm, layer, is_byte = _get_context(context)
        if _check_domain(self, obj, bm):
            return {'CANCELLED'}

        if self.source == 'ACTIVE':
            if not bm.faces.active or not bm.faces.active.select:
                self.report({'ERROR'}, "No active face selected")
                return {'CANCELLED'}
            loops = list(bm.faces.active.loops)
        else:
            bm.faces.ensure_lookup_table()
            loops = _target_loop_list(bm, _collect_target_loops(bm, context))
            if not loops:
                self.report({'ERROR'}, "No elements selected")
                return {'CANCELLED'}

        context.scene.vcem.color = _from_layer_space(
            _average_color(loops, layer), is_byte)
        return {'FINISHED'}


class MESH_OT_VCEM_SelectSameColor(bpy.types.Operator):
    """Select all faces whose color matches the active face (within tolerance)"""
    bl_idname = "mesh.vcem_select_same_color"
    bl_label = "Select Same Color"
    bl_options = {'REGISTER', 'UNDO'}

    tolerance: bpy.props.FloatProperty(
        name="Tolerance",
        description="Maximum per-channel difference to count as the same color",
        default=0.01, min=0.0, max=1.0, precision=4)

    @classmethod
    def poll(cls, context):
        return (context.active_object and context.active_object.type == 'MESH'
                and context.active_object.mode == 'EDIT')

    def execute(self, context):
        obj, bm, layer, is_byte = _get_context(context)
        if _check_domain(self, obj, bm):
            return {'CANCELLED'}

        active = bm.faces.active
        if active is None:
            self.report({'ERROR'}, "No active face")
            return {'CANCELLED'}

        ref = _average_color(list(active.loops), layer)
        tol = self.tolerance
        count = 0
        for f in bm.faces:
            c = _average_color(list(f.loops), layer)
            match = all(abs(c[i] - ref[i]) <= tol for i in range(3))
            f.select = match
            count += match

        # Selection-only change: skip triangle rebuild and geometry flush
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        self.report({'INFO'}, f"Selected {count} faces")
        return {'FINISHED'}


def _collect_target_loops(bm, context):
    """Return [(face_index, [loop_positions])] for the current selection,
    respecting the mesh selection mode (vert/edge selections grab the loops
    of all linked faces, which is what makes gradients possible)."""
    select_mode = context.tool_settings.mesh_select_mode

    if select_mode[2]:  # FACE
        return [(f.index, list(range(len(f.loops))))
                for f in bm.faces if f.select]

    verts = set()
    if select_mode[0]:  # VERT
        verts = {v for v in bm.verts if v.select}
    if select_mode[1]:  # EDGE
        for e in bm.edges:
            if e.select:
                verts.update(e.verts)

    target_loops = set()
    for v in verts:
        target_loops.update(v.link_loops)

    result = []
    for f in bm.faces:
        positions = [i for i, loop in enumerate(f.loops) if loop in target_loops]
        if positions:
            result.append((f.index, positions))
    return result


class MESH_OT_VCEM_InteractiveAdjust(bpy.types.Operator):
    """Interactively adjust the hue/saturation/value of the selection (faces,
    or loops of selected verts/edges for gradients).

    H/S/V: pick what mouse X adjusts  |  Shift: fine adjustment  |
    Type digits: set value numerically  |  O: toggle overlays  |
    LMB/Enter: confirm  |  RMB/Esc: cancel.
    After confirming, use F9 (Adjust Last Operation) to tweak the result.
    """
    bl_idname = "mesh.vcem_interactive_adjust"
    bl_label = "Interactive Hue/Value"
    bl_options = {'REGISTER', 'UNDO', 'GRAB_CURSOR', 'BLOCKING'}

    SENSITIVITY = 0.005

    # Redo properties (shown in the F9 "Adjust Last Operation" dialog).
    # The modal writes its final result here on confirm; execute() replays it.
    hue: bpy.props.FloatProperty(
        name="Hue", description="Hue shift applied to the selection",
        min=-1.0, max=1.0, default=0.0)
    saturation: bpy.props.FloatProperty(
        name="Saturation", description="Saturation shift applied to the selection",
        min=-1.0, max=1.0, default=0.0)
    value: bpy.props.FloatProperty(
        name="Value", description="Value (brightness) shift applied to the selection",
        min=-1.0, max=1.0, default=0.0)
    absolute_value: bpy.props.FloatProperty(
        name="Absolute Value",
        description="Value set numerically during the modal (overrides the Value shift)",
        min=0.0, max=1.0, default=0.5)
    use_absolute_value: bpy.props.BoolProperty(
        name="Use Absolute Value", default=False, options={'HIDDEN'})

    _NUM_KEYS = {
        'ZERO': '0', 'ONE': '1', 'TWO': '2', 'THREE': '3', 'FOUR': '4',
        'FIVE': '5', 'SIX': '6', 'SEVEN': '7', 'EIGHT': '8', 'NINE': '9',
        'PERIOD': '.', 'NUMPAD_PERIOD': '.',
        **{f'NUMPAD_{i}': str(i) for i in range(10)},
    }

    _MODE_KEYS = {'H': 'HUE', 'S': 'SATURATION', 'V': 'VALUE'}

    @classmethod
    def poll(cls, context):
        return (context.active_object and context.active_object.type == 'MESH'
                and context.active_object.mode == 'EDIT')

    # -- helpers ---------------------------------------------------------

    def _header(self, context, hsv_sample):
        h, s, v = hsv_sample
        typed = self.num_input or "—"
        overlays = "ON" if context.space_data.overlay.show_overlays else "OFF"
        text = (f"Mode: {self.mode} (H/S/V to switch)   •   "
                f"Hue: {h:.3f}   Sat: {s:.3f}   Value: {v:.3f}   •   "
                f"Shift: Fine   •   Type Value: {typed}   •   "
                f"O: Overlays ({overlays})   •   "
                f"LMB/Enter: Confirm   RMB/Esc: Cancel")
        context.area.header_text_set(text)

    def _fresh_bmesh(self):
        """Re-grab the edit bmesh. Stored BMesh/BMLoop references go stale
        because Blender pushes an undo step (rebuilding the bmesh) while the
        modal is running, so we re-fetch everything by face index each time.
        (BMLoopSeq has no lookup table, so we key off faces instead.)"""
        bm = bmesh.from_edit_mesh(self.obj.data)
        bm.faces.ensure_lookup_table()
        # The bmesh may have been rebuilt from mesh data by an undo push,
        # losing a layer created during invoke — re-create it if needed
        layer = _find_loop_layer(bm, self.layer_name, self.layer_data_type)
        if layer is None:
            layer = bm.loops.layers.color.active
        return bm, layer

    def _typed_value(self):
        if not self.num_input:
            return None
        try:
            return max(0.0, min(1.0, float(self.num_input)))
        except ValueError:
            self.num_input = ""
            return None

    def _apply(self, context):
        typed = self._typed_value()

        bm, layer = self._fresh_bmesh()
        sample = None
        for face_index, entries in self.initial_colors:
            loops = list(bm.faces[face_index].loops)
            for pos, initial in entries:
                h, s, v = colorsys.rgb_to_hsv(*initial[:3])
                h = (h + self.hue_delta) % 1.0
                s = max(0.0, min(1.0, s + self.sat_delta))
                v = typed if typed is not None \
                    else max(0.0, min(1.0, v + self.value_delta))
                loops[pos][layer] = (*colorsys.hsv_to_rgb(h, s, v), initial[3])
                if sample is None:
                    sample = (h, s, v)

        bmesh.update_edit_mesh(self.obj.data)
        if sample:
            self._header(context, sample)

    # -- operator --------------------------------------------------------

    def modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            dx = event.mouse_x - self.mouse_prev_x
            self.mouse_prev_x = event.mouse_x
            sensitivity = self.SENSITIVITY * (0.1 if event.shift else 1.0)
            if self.mode == 'HUE':
                self.hue_delta += dx * sensitivity
            elif self.mode == 'SATURATION':
                self.sat_delta += dx * sensitivity
            else:
                self.value_delta += dx * sensitivity
            self._apply(context)

        elif event.value == 'PRESS' and event.type in self._MODE_KEYS:
            self.mode = self._MODE_KEYS[event.type]
            self._apply(context)  # refresh header only (deltas unchanged)

        elif event.type == 'O' and event.value == 'PRESS':
            overlay = context.space_data.overlay
            overlay.show_overlays = not overlay.show_overlays
            context.area.tag_redraw()
            self._apply(context)

        elif event.value == 'PRESS' and event.type in self._NUM_KEYS:
            ch = self._NUM_KEYS[event.type]
            if ch != '.' or '.' not in self.num_input:
                self.num_input += ch
            self.mode = 'VALUE'
            self._apply(context)

        elif event.type == 'BACK_SPACE' and event.value == 'PRESS':
            self.num_input = self.num_input[:-1]
            self._apply(context)

        elif event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            # Store the result in the redo properties so F9 can replay/tweak it
            self.hue = ((self.hue_delta + 1.0) % 2.0) - 1.0
            self.saturation = max(-1.0, min(1.0, self.sat_delta))
            self.value = max(-1.0, min(1.0, self.value_delta))
            typed = self._typed_value()
            self.use_absolute_value = typed is not None
            if typed is not None:
                self.absolute_value = typed
            self._restore_overlay(context)
            context.area.header_text_set(None)
            return {'FINISHED'}

        elif event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            bm, layer = self._fresh_bmesh()
            for face_index, entries in self.initial_colors:
                loops = list(bm.faces[face_index].loops)
                for pos, initial in entries:
                    loops[pos][layer] = initial
            bmesh.update_edit_mesh(self.obj.data)
            self._restore_overlay(context)
            context.area.header_text_set(None)
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def _restore_overlay(self, context):
        overlay = context.space_data.overlay
        if overlay.show_overlays != self.overlay_prev:
            overlay.show_overlays = self.overlay_prev
            context.area.tag_redraw()

    def execute(self, context):
        """Replay for the redo (F9) panel: Blender restores the pre-operator
        state first, so we re-apply the stored shift to the current colors."""
        obj, bm, layer, _ = _get_context(context)
        if _check_domain(self, obj, bm):
            return {'CANCELLED'}

        bm.faces.ensure_lookup_table()
        for face_index, positions in _collect_target_loops(bm, context):
            loops = list(bm.faces[face_index].loops)
            for pos in positions:
                loop = loops[pos]
                c = loop[layer]
                h, s, v = colorsys.rgb_to_hsv(*c[:3])
                h = (h + self.hue) % 1.0
                s = max(0.0, min(1.0, s + self.saturation))
                v = self.absolute_value if self.use_absolute_value \
                    else max(0.0, min(1.0, v + self.value))
                loop[layer] = (*colorsys.hsv_to_rgb(h, s, v), c[3])

        bmesh.update_edit_mesh(obj.data)
        return {'FINISHED'}

    def invoke(self, context, event):
        self.obj, bm, layer, is_byte = _get_context(context)
        if _check_domain(self, self.obj, bm):
            return {'CANCELLED'}

        bm.faces.ensure_lookup_table()
        self.layer_name = layer.name
        self.layer_data_type = 'BYTE_COLOR' if is_byte else 'FLOAT_COLOR'
        self.initial_colors = []
        for face_index, positions in _collect_target_loops(bm, context):
            loops = list(bm.faces[face_index].loops)
            self.initial_colors.append((
                face_index,
                [(pos, tuple(loops[pos][layer])) for pos in positions],
            ))
        if not self.initial_colors:
            self.report({'ERROR'}, "No elements selected")
            return {'CANCELLED'}

        # Hide overlays while the tool runs (O toggles); restored on exit
        overlay = context.space_data.overlay
        self.overlay_prev = overlay.show_overlays
        if overlay.show_overlays:
            overlay.show_overlays = False
            context.area.tag_redraw()
        self.mode = 'VALUE'
        self.hue_delta = 0.0
        self.sat_delta = 0.0
        self.value_delta = 0.0
        self.mouse_prev_x = event.mouse_x
        self.num_input = ""

        context.window_manager.modal_handler_add(self)
        self._apply(context)  # draw header immediately
        return {'RUNNING_MODAL'}


# -----------------------------------------------------------------------------
# Panel
# -----------------------------------------------------------------------------

class VIEW3D_PT_VCEM_Panel(bpy.types.Panel):
    bl_label = "Vertex Color - Edit Mode"
    bl_idname = "VIEW3D_PT_vcem_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Vertex Color'
    bl_context_mode = 'EDIT_MESH'

    def draw(self, context):
        layout = self.layout
        props = context.scene.vcem

        # Color wheel
        layout.template_color_picker(props, "color", value_slider=True)
        row = layout.row(align=True)
        row.prop(props, "color", text="")
        row.operator("mesh.vcem_randomize_wheel", icon='FILE_REFRESH', text="")

        # Sample into wheel
        row = layout.row(align=True)
        row.operator("mesh.vcem_sample_wheel", text="Pick from Active").source = 'ACTIVE'
        row.operator("mesh.vcem_sample_wheel", text="Pick Average").source = 'AVERAGE'
        layout.operator("mesh.vcem_select_same_color",
                        icon='RESTRICT_SELECT_OFF', text="Select Same Color")

        layout.separator()

        # Apply
        box = layout.box()
        box.prop(props, "split_faces")
        row = box.row()
        row.scale_y = 2.0
        row.operator("mesh.vcem_apply_color", icon='BRUSH_DATA',
                     text="Apply Wheel Color").source = 'WHEEL'
        row = box.row(align=True)
        row.operator("mesh.vcem_apply_color",
                     text="Apply Active Face").source = 'ACTIVE'
        row.operator("mesh.vcem_apply_color",
                     text="Apply Average").source = 'AVERAGE'

        layout.separator()
        layout.operator("mesh.vcem_interactive_adjust",
                        icon='MOUSE_MOVE', text="Interactive Hue/Value")

        # Color attributes
        obj = context.active_object
        if obj and obj.type == 'MESH':
            box = layout.box()
            box.label(text="Color Attributes", icon='GROUP_VCOL')
            row = box.row()
            mesh = obj.data
            row.template_list("MESH_UL_color_attributes", "color_attributes",
                              mesh, "color_attributes",
                              mesh.color_attributes, "active_color_index",
                              rows=3)
            col = row.column(align=True)
            col.operator("geometry.color_attribute_add", icon='ADD', text="")
            col.operator("geometry.color_attribute_remove", icon='REMOVE', text="")
            col.separator()
            col.menu("MESH_MT_color_attribute_context_menu",
                     icon='DOWNARROW_HLT', text="")
            if len(mesh.color_attributes):
                col.operator("geometry.color_attribute_render_set",
                             icon='RESTRICT_RENDER_OFF', text="")
            attr = obj.data.color_attributes.active
            if attr:
                box.label(text=f"Domain: {attr.domain.title()}  •  "
                               f"{'Byte (sRGB)' if attr.data_type == 'BYTE_COLOR' else 'Float (linear)'}",
                          icon='INFO')


# -----------------------------------------------------------------------------
# Registration
# -----------------------------------------------------------------------------

classes = (
    VCEM_Properties,
    MESH_OT_VCEM_ApplyColor,
    MESH_OT_VCEM_RandomizeWheel,
    MESH_OT_VCEM_SampleWheel,
    MESH_OT_VCEM_SelectSameColor,
    MESH_OT_VCEM_InteractiveAdjust,
    VIEW3D_PT_VCEM_Panel,
)

addon_keymaps = []


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.vcem = bpy.props.PointerProperty(type=VCEM_Properties)

    # Ctrl+Shift+E -> Interactive Hue/Value (Edit Mode)
    keyconfigs = bpy.context.window_manager.keyconfigs.addon
    if keyconfigs:
        km = keyconfigs.keymaps.new(name='Mesh', space_type='EMPTY')
        kmi = km.keymap_items.new(MESH_OT_VCEM_InteractiveAdjust.bl_idname,
                                  'E', 'PRESS', ctrl=True, shift=True)
        addon_keymaps.append((km, kmi))


def unregister():
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()

    del bpy.types.Scene.vcem
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
