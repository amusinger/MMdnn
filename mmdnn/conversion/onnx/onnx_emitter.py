from mmdnn.conversion.common.DataStructure.emitter import Emitter
from mmdnn.conversion.common.IR.IR_graph import IRGraph
import os.path
import mmdnn.conversion.common.IR.graph_pb2 as graph_pb2


class OnnxEmitter(Emitter):
    dtype_map = {
        graph_pb2.DT_FLOAT32: "TensorProto.FLOAT"
    }

    def __init__(self, architecture, weight):
        super(OnnxEmitter, self).__init__()
        if os.path.exists(architecture) == False:
            raise ValueError("IR architecture file [{}] is not found.".format(architecture))
        else:
            self.IR_graph = IRGraph(architecture)
            self.IR_graph.build()

        if os.path.exists(weight) == False:
            raise ValueError("IR weight file [{}] is not found.".format(weight))
        else:
            self._load_weights(weight)

    @staticmethod
    def _shapeToStr(shapes):
        ret = [dim.size if dim.size != -1 else 1 for dim in shapes.dim]
        return ', '.join('%s' % i for i in ret)

    @property
    def header_code(self):
        return """import numpy as np
from onnx import helper, TensorProto
import onnx

__weights_dict = dict()

def load_weights(weight_file):
    if weight_file == None:
        return

    try:
        weights_dict = np.load(weight_file).item()
    except:
        weights_dict = np.load(weight_file, encoding='bytes').item()

    return weights_dict


def KitModel(weight_file = None):
    global __weights_dict
    __weights_dict = load_weights(weight_file)

"""

    def gen_code(self, phase):
        self.add_body(0, self.header_code)

        self.inputs = []
        self.outputs = []
        self.nodes = []
        self.initializer = []

        for layer in self.IR_graph.topological_sort:
            current_node = self.IR_graph.get_node(layer)
            node_type = current_node.type

            if hasattr(self, "emit_" + node_type):
                func = getattr(self, "emit_" + node_type)
                func(current_node)
            else:
                print("OnnxEmitter has not supported operator [%s]." % (node_type))
                self.emit_UNKNOWN(current_node)

        self._process_output_layers()

        self.add_body(1, "graph = helper.make_graph([{}], 'mmdnn', [{}], [{}], [{}])".format(', '.join(self.nodes),
                                                                                             ', '.join(self.inputs),
                                                                                             ', '.join(self.outputs),
                                                                                             ', '.join(
                                                                                                 self.initializer))
                      )
        self.add_body(1, "return helper.make_model(graph)")
        return self.body_code

    def _process_output_layers(self):
        for name in self.IR_graph.output_layers:
            IR_node = self.IR_graph.get_node(name)
            shape_str = IRGraph.shapeToStr(IR_node.layer.attr["_output_shapes"].list.shape[0])
            if IR_node.layer.attr['dtype'].type == graph_pb2.DT_UNDEFINED:
                IR_node.layer.attr['dtype'].type = graph_pb2.DT_FLOAT32
            dtype_str = self.dtype_map[IR_node.layer.attr['dtype'].type]
            self.add_body(1, "{:<15} = helper.make_tensor_value_info('{}', {}, ({},))".format(
                IR_node.variable_name + '_out',
                IR_node.variable_name,
                dtype_str,
                shape_str))
            self.outputs.append(IR_node.variable_name + '_out')

    def emit_DataInput(self, IR_node):
        shape_str = self._shapeToStr(IR_node.IR_layer.attr["shape"].shape)
        dtype_str = self.dtype_map[IR_node.layer.attr['dtype'].type]
        self.add_body(1, "{:<15} = helper.make_tensor_value_info('{}', {}, ({},))".format(
            IR_node.variable_name,
            IR_node.variable_name,
            dtype_str,
            shape_str))
        self.inputs.append(IR_node.variable_name)

    def emit_Conv(self, IR_node):
        dilations = list(IR_node.get_attr('dilations'))[1:-1]
        group = IR_node.get_attr('group', 1)
        kernel_shape = list(IR_node.get_attr('kernel_shape'))[:2]
        pads = IR_node.get_attr('pads')
        pad_length = len(pads)
        pads = pads[1:pad_length // 2 - 1] + pads[pad_length // 2 + 1:pad_length - 1]
        strides = list(IR_node.get_attr('strides'))[1:-1]
        self.add_body(1, "{:15} = __weights_dict['{}']['weights']".format(
            IR_node.variable_name + '_weight_array',
            IR_node.name))
        self.add_body(1, "{} = {}.transpose([3,2,0,1])".format(
            IR_node.variable_name + '_weight_array',
            IR_node.variable_name + '_weight_array'))
        self.add_body(1, "{:15} = helper.make_node('Constant', inputs=[], outputs=['{}'], value=helper.make_tensor(name='const_tensor', data_type=onnx.mapping.NP_TYPE_TO_TENSOR_TYPE[{}.dtype], dims={}.shape, vals={}.flatten().astype(float)))".format(
                          IR_node.variable_name + '_weight',
                          IR_node.variable_name + '_weight',
                          IR_node.variable_name + '_weight_array',
                          IR_node.variable_name + '_weight_array',
                          IR_node.variable_name + '_weight_array'))
        self.add_body(1, "{:15} = helper.make_node('Conv', inputs=['{}', '{}'],outputs=['{}'], dilations={}, group={}, kernel_shape={}, pads={}, strides={})".format(
                          IR_node.variable_name,
                          self.parent_variable_name(IR_node),
                          IR_node.variable_name + '_weight',
                          IR_node.variable_name,
                          dilations,
                          group,
                          kernel_shape,
                          pads,
                          strides))
        self.nodes.append(IR_node.variable_name + '_weight')
        self.nodes.append(IR_node.variable_name)

    def emit_BatchNorm(self, IR_node):
        epsilon = IR_node.get_attr('epsilon')
        self.add_body(1, "{:15} = __weights_dict['{}']['scale']".format(IR_node.variable_name + '_scale_array',
                                                                        IR_node.name))
        self.add_body(1, "{:15} = helper.make_node('Constant', inputs=[], outputs=['{}'], value=helper.make_tensor(name='const_tensor', data_type=onnx.mapping.NP_TYPE_TO_TENSOR_TYPE[{}.dtype], dims={}.shape, vals={}))".format(
                          IR_node.variable_name + '_scale',
                          IR_node.variable_name + '_scale',
                          IR_node.variable_name + '_scale_array',
                          IR_node.variable_name + '_scale_array',
                          IR_node.variable_name + '_scale_array'))
        self.add_body(1, "{:15} = __weights_dict['{}']['bias']".format(
            IR_node.variable_name + '_bias_array',
            IR_node.name))
        self.add_body(1, "{:15} = helper.make_node('Constant', inputs=[], outputs=['{}'], value=helper.make_tensor(name='const_tensor', data_type=onnx.mapping.NP_TYPE_TO_TENSOR_TYPE[{}.dtype], dims={}.shape, vals={}))".format(
                          IR_node.variable_name + '_bias',
                          IR_node.variable_name + '_bias',
                          IR_node.variable_name + '_bias_array',
                          IR_node.variable_name + '_bias_array',
                          IR_node.variable_name + '_bias_array'))
        self.add_body(1, "{:15} = __weights_dict['{}']['mean']".format(
            IR_node.variable_name + '_mean_array',
            IR_node.name))
        self.add_body(1, "{:15} = helper.make_node('Constant', inputs=[], outputs=['{}'], value=helper.make_tensor(name='const_tensor', data_type=onnx.mapping.NP_TYPE_TO_TENSOR_TYPE[{}.dtype], dims={}.shape, vals={}))".format(
                          IR_node.variable_name + '_mean',
                          IR_node.variable_name + '_mean',
                          IR_node.variable_name + '_mean_array',
                          IR_node.variable_name + '_mean_array',
                          IR_node.variable_name + '_mean_array'))
        self.add_body(1, "{:15} = __weights_dict['{}']['var']".format(
                          IR_node.variable_name + '_var_array',
                          IR_node.name))
        self.add_body(1, "{:15} = helper.make_node('Constant', inputs=[], outputs=['{}'], value=helper.make_tensor(name='const_tensor', data_type=onnx.mapping.NP_TYPE_TO_TENSOR_TYPE[{}.dtype], dims={}.shape, vals={}))".format(
                          IR_node.variable_name + '_var',
                          IR_node.variable_name + '_var',
                          IR_node.variable_name + '_var_array',
                          IR_node.variable_name + '_var_array',
                          IR_node.variable_name + '_var_array'))
        self.add_body(1, "{:15} = helper.make_node('BatchNormalization', inputs=['{}', '{}', '{}', '{}', '{}'],outputs=['{}'], epsilon={})".format(
                          IR_node.variable_name,
                          self.parent_variable_name(IR_node),
                          IR_node.variable_name + '_scale',
                          IR_node.variable_name + '_bias',
                          IR_node.variable_name + '_mean',
                          IR_node.variable_name + '_var',
                          IR_node.variable_name,
                          epsilon))
        self.nodes.append(IR_node.variable_name + '_scale')
        self.nodes.append(IR_node.variable_name + '_bias')
        self.nodes.append(IR_node.variable_name + '_mean')
        self.nodes.append(IR_node.variable_name + '_var')
        self.nodes.append(IR_node.variable_name)

    def emit_Relu(self, IR_node):
        self.add_body(1, "{:15} = helper.make_node('Relu', inputs=['{}'], outputs=['{}'])".format(
            IR_node.variable_name,
            self.parent_variable_name(IR_node),
            IR_node.variable_name))
        self.nodes.append(IR_node.variable_name)

    def emit_Add(self, IR_node):
        input_layers = ', '.join(
            ("'" + self.IR_graph.get_parent(IR_node.variable_name, [num]).real_variable_name) + "'" for num in
            range(0, len(IR_node.in_edges)))
        self.add_body(1, "{:15} = helper.make_node('Add', inputs=[{}], outputs=['{}'])".format(
            IR_node.variable_name,
            input_layers,
            IR_node.variable_name))
        self.nodes.append(IR_node.variable_name)

    def emit_Pool(self, IR_node):
        pooling_type = IR_node.get_attr('pooling_type')
        if IR_node.layer.attr['global_pooling'].b:
            if pooling_type == 'AVG':
                self.add_body(1, "{:15} = helper.make_node('GlobalAveragePool', inputs=['{}'], outputs=['{}'])".format(
                    IR_node.variable_name,
                    self.parent_variable_name(IR_node),
                    IR_node.variable_name))
                self.nodes.append(IR_node.variable_name)
            else:
                print("OnnxEmitter has not supported Global Pool type [%s]." % (pooling_type))
                self.emit_UNKNOWN(IR_node)
        else:
            if pooling_type in ['AVG', 'MAX']:
                if pooling_type == 'AVG':
                    op_name = 'AveragePool'
                elif pooling_type == 'MAX':
                    op_name = 'MaxPool'
                kernel_shape = list(IR_node.get_attr('kernel_shape')[1:-1])
                pads = IR_node.get_attr('pads')
                pad_length = len(pads)
                pads = pads[1:pad_length // 2 - 1] + pads[pad_length // 2 + 1:pad_length - 1]
                strides = list(IR_node.get_attr('strides')[1:-1])
                self.add_body(1, "{:15} = helper.make_node('{}', inputs=['{}'],outputs=['{}'], kernel_shape={}, pads={}, strides={})".format(
                                  IR_node.variable_name,
                                  op_name,
                                  self.parent_variable_name(IR_node),
                                  IR_node.variable_name,
                                  kernel_shape,
                                  pads,
                                  strides))
                self.nodes.append(IR_node.variable_name)
            else:
                print("OnnxEmitter has not supported Pool type [%s]." % (pooling_type))
                self.emit_UNKNOWN(IR_node)

    def emit_FullyConnected(self, IR_node):
        self.add_body(1, "{:15} = __weights_dict['{}']['weights']".format(
            IR_node.variable_name + '_weight_array',
            IR_node.name))
        self.add_body(1, "{:15} = helper.make_node('Constant', inputs=[], outputs=['{}'], value=helper.make_tensor(name='const_tensor', data_type=onnx.mapping.NP_TYPE_TO_TENSOR_TYPE[{}.dtype], dims={}.shape, vals={}.flatten().astype(float)))".format(
                          IR_node.variable_name + '_weight',
                          IR_node.variable_name + '_weight',
                          IR_node.variable_name + '_weight_array',
                          IR_node.variable_name + '_weight_array',
                          IR_node.variable_name + '_weight_array'))
        self.add_body(1, "{:15} = __weights_dict['{}']['bias']".format(
            IR_node.variable_name + '_bias_array',
            IR_node.name))
        self.add_body(1, "{:15} = helper.make_node('Constant', inputs=[], outputs=['{}'], value=helper.make_tensor(name='const_tensor', data_type=onnx.mapping.NP_TYPE_TO_TENSOR_TYPE[{}.dtype], dims={}.shape, vals={}.flatten().astype(float)))".format(
                          IR_node.variable_name + '_bias',
                          IR_node.variable_name + '_bias',
                          IR_node.variable_name + '_bias_array',
                          IR_node.variable_name + '_bias_array',
                          IR_node.variable_name + '_bias_array'))
        self.add_body(1, "{:15} = helper.make_node('Gemm', inputs=['{}', '{}', '{}'],outputs=['{}'])".format(
            IR_node.variable_name,
            self.parent_variable_name(IR_node),
            IR_node.variable_name + '_weight',
            IR_node.variable_name + '_bias',
            IR_node.variable_name))
        self.nodes.append(IR_node.variable_name + '_weight')
        self.nodes.append(IR_node.variable_name + '_bias')
        self.nodes.append(IR_node.variable_name)

    def emit_Pad(self, IR_node):
        mode = IR_node.layer.attr['mode'].s.decode()
        pads = IR_node.get_attr('pads')
        pad_length = len(pads)
        pads = [0, 0] + pads[1:pad_length // 2 - 1] + [0, 0] + pads[pad_length // 2 + 1:pad_length - 1]
        self.add_body(1, "{:15} = helper.make_node('Pad', inputs=['{}'], outputs=['{}'], mode='{}', pads={})".format(
            IR_node.variable_name,
            self.parent_variable_name(IR_node),
            IR_node.variable_name,
            mode,
            pads))
        self.nodes.append(IR_node.variable_name)

    def emit_Concat(self, IR_node):
        axis = IR_node.get_attr('axis')
        inputs = ', '.join("'" + self.IR_graph.get_node(i).real_variable_name + "'" for i in IR_node.in_edges)
        self.add_body(1, "{:15} = helper.make_node('Concat', inputs=[{}], outputs=['{}'], axis={})".format(
            IR_node.variable_name,
            inputs,
            IR_node.variable_name,
            axis))
        self.nodes.append(IR_node.variable_name)

    def emit_Flatten(self, IR_node):
        self.add_body(1, "{:15} = helper.make_node('Flatten', inputs=['{}'], outputs=['{}'])".format(
            IR_node.variable_name,
            self.parent_variable_name(IR_node),
            IR_node.variable_name))
        self.nodes.append(IR_node.variable_name)

    def emit_Softmax(self, IR_node):
        self.add_body(1, "{:15} = helper.make_node('Softmax', inputs=['{}'], outputs=['{}'])".format(
            IR_node.variable_name,
            self.parent_variable_name(IR_node),
            IR_node.variable_name))
        self.nodes.append(IR_node.variable_name)

    def emit_UNKNOWN(self, IR_node):
        print(IR_node.IR_layer.name)
