'''
- This file contains the customization of the nn layers that are to be used in the model 
- The customization can be summarized in adding customized forward functions with the sparsity mask included, 
functions for the LUT-based model to generate the truth tables and to carry the forward functions, and lastly, functions to write the layer 
contents into verilog for later hardware analysis. 
- The majority of the content in the file is adapted from LogicNets repository with some modification to make the code compatable with the CNN processing. 
'''

# Imports
from functools import partial, reduce

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import nn, Tensor
from torch.nn import init
from torch.nn.parameter import Parameter

import brevitas.nn as qnn

from init import random_restrict_fanin
from utils import fetch_mask_indices, fetch_mask_indices_edited, generate_permutation_matrix
from verilog import    generate_lut_verilog, \
                        generate_neuron_connection_verilog, \
                        layer_connection_verilog, \
                        generate_logicnets_verilog, \
                        generate_register_verilog
from bench import      generate_lut_bench, \
                        generate_lut_input_string, \
                        sort_to_bench


# functions for the truth table generations and the Verilog generation: 
def generate_truth_tables(model: nn.Module, verbose: bool = False): 
    
    training = model.training 
    model.eval() 
    for name, module in model.named_modules():
        if type(module) == SparseLinearNeq: # Generating truth tables for Linear FC layers 
            if verbose: 
                print(f"Generating truth table for layer {name}")
            module.calculate_truth_tables()
            print(f'truth table done for layer {name}')
            if verbose: 
                print(f"Truth tables generated for {len(module.neuron_truth_tables)} neurons")

        if type(module) == SparseConv1dNeq: # Generating truth tables for convolutional layers
            if verbose:
                print(f"Generating truth table for layer {name}")
            module.calculate_truth_tables_conv2()
            if verbose:
                print(f"Truth tables generated for {len(module.out_channel_truth_table)} channels") 

    model.training = training


def lut_inference(model: nn.Module) -> None: 
    for name, module in model.named_modules(): 
        if type(module) == SparseLinearNeq: 
            module.lut_inference()

        if  type(module) == SparseConv1dNeq:
            module.lut_inference()

def neq_inference(model: nn.Module) -> None: 
    for name, module in model.named_modules():
        if type(module) == SparseLinearNeq: 
            module.neq_inference()

        if  type(module) == SparseConv1dNeq:
            module.neq_inference()


# the function belos is to transform the model into verilog module. (conv layers are to be added into the function) 
def module_list_to_verilog_module(module_list: nn.ModuleList, module_name: str, output_directory: str, add_registers: bool = True, generate_bench: bool =True): 
    input_bitwidth = None 
    output_bitwidth = None
    module_contents = ''

    for i in range(len(module_list)):
        m = module_list[i]
        # print(f'layer {m} and layer type is {type(m)}')
        if isinstance(m, SparseLinearNeq):  # replace with SparseLinearNeq
            module_prefix = f"layer{i}"
            module_input_bits, module_output_bits = m.gen_layer_verilog(module_prefix, output_directory, generate_bench=generate_bench)
            if i == 0:
                input_bitwidth = module_input_bits  # this might be deleted from here and be only defined at the beginning of the model as the model input bits starts as layer 0 which is a conv layer
            if i == len(module_list)-1: 
                output_bitwidth = module_output_bits 
            
            module_contents += layer_connection_verilog( module_prefix, 
                                                        input_string = f'M{i}', 
                                                        input_bits = module_input_bits, 
                                                        output_string = f'M{i+1}',
                                                        output_bits = module_output_bits,
                                                        output_wire = i !=len(module_list)-1, 
                                                        register = add_registers) 
        
        if isinstance(m, SparseConv1dNeq): #type(m) == SparseConv1dNeq
            module_prefix = f"layer{i}"
            module_input_bits, module_output_bits = m.gen_layer_verilog(module_prefix, output_directory, generate_bench=generate_bench)
            if i == 0:
                input_bitwidth = module_input_bits
            if i == len(module_list)-1: 
                output_bitwidth = module_output_bits   # this might also be deleted from here and be identified only in the linear layers as the model output bitwidth can only be obtained at the last layer which is a sparse linear layer
            
            module_contents += layer_connection_verilog( module_prefix, 
                                                        input_string = f'M{i}', 
                                                        input_bits = module_input_bits, 
                                                        output_string = f'M{i+1}',
                                                        output_bits = module_output_bits,
                                                        output_wire = i !=len(module_list)-1, 
                                                        register = add_registers)
        
        # else:  
        #     raise Exception(f'Expect type(module) == SparseLinearNeq or SparseConv1dNeq, {type(m)} found') 
        
    module_list_verilog = generate_logicnets_verilog( module_name = module_name, 
                                                     input_name = 'M0', 
                                                     input_bits = input_bitwidth, 
                                                     output_name = f'M{len(module_list)}', 
                                                     output_bits = output_bitwidth, 
                                                     module_contents = module_contents)
    
    reg_verilog = generate_register_verilog() 
    with open(f"{output_directory}/myreg.v", 'w') as f: 
        f.write(reg_verilog)

    with open(f'{output_directory}/{module_name}.v', 'w') as f: 
        f.write(module_list_verilog) 


       
# The classes below are the customized layers build.
class SparseLinear(qnn.QuantLinear): 
    def __init__(self, in_features: int, out_features: int, mask: nn.Module, bias: bool = False) -> None:
        super(SparseLinear, self).__init__(in_features=in_features, out_features=out_features, bias=bias)
        self.mask = mask

    def forward(self, input: Tensor) -> torch.Tensor:
        return F.linear(input, self.weight * self.mask(), self.bias)


class SparseLinearNeq(nn.Module):
    def __init__(self, in_features: int, out_features: int, input_quant, output_quant, mask, reshaped_in_features=0, apply_input_quant=True, apply_output_quant=True, first_linear=False, bias = False) -> None:
        super(SparseLinearNeq, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.input_quant = input_quant
        self.fc = SparseLinear(in_features, out_features, mask, bias)
        self.output_quant = output_quant
        self.is_lut_inference = False
        self.neuron_truth_tables = None
        self.apply_input_quant = apply_input_quant
        self.apply_output_quant = apply_output_quant
        self.first_linear = first_linear
        self.reshaped_in_features = reshaped_in_features


    def lut_cost(self):
        """
        Approximate how many 6:1 LUTs are needed to implement this layer using 
        LUTCost() as defined in LogicNets paper FPL'20:
            LUTCost(X, Y) = (Y / 3) * (2^(X - 4) - (-1)^X)
        where:
        * X: input fanin bits
        * Y: output bits 
        LUTCost() estimates how many LUTs are needed to implement 1 neuron, so 
        we then multiply LUTCost() by the number of neurons to get the total 
        number of LUTs needed.
        NOTE: This function (over)estimates how many 6:1 LUTs are needed to implement
        this layer b/c it assumes every neuron is connected to the next layer 
        since we do not have the next layer's sparsity information.
        """

        # Compute LUTCost of 1 neuron
        _, input_bitwidth = self.input_quant.get_scale_factor_bits()
        _, output_bitwidth = self.output_quant.get_scale_factor_bits()
        input_bitwidth, output_bitwidth = int(input_bitwidth), int(output_bitwidth)
        x = input_bitwidth * self.fc.mask.fan_in # neuron input fanin
        y = output_bitwidth 
        neuron_lut_cost = (y / 3) * ((2 ** (x - 4)) - ((-1) ** x))
        # Compute total LUTCost
        return self.out_features * neuron_lut_cost
    

    def gen_layer_verilog(self, module_prefix, directory, generate_bench: bool = True): 

        _, input_bitwidth = self.input_quant.get_scale_factor_bits()
        _, output_bitwidth = self.output_quant.get_scale_factor_bits()
        input_bitwidth, output_bitwidth = int(input_bitwidth), int(output_bitwidth)
        total_input_bits = self.in_features * input_bitwidth
        total_output_bits = self.out_features * output_bitwidth

        # The line below takes the module_prefix which is a (layer number or name) and print the input and output bitwidth
        layer_contents = f"module {module_prefix} (input [{total_input_bits-1}:0] M0, output[{total_output_bits-1}:0] M1); \n\n"
        output_offset = 0 

        for index in range(self.out_features): 
            module_name = f"{module_prefix}_N{index}" 
            indices, _, _, _ = self.neuron_truth_tables[index]
            neuron_verilog = self.gen_neuron_verilog(index, module_name) 

            with open(f"{directory}/{module_name}.v", "w") as f: 
                f.write(neuron_verilog)
            
            if generate_bench: 
                # Generate the contents of the neuron verilog
                neuron_bench = self.gen_neuron_bench(index, module_name)
                with open(f"{directory}/{module_name}.bench", "w") as f: 
                    f.write(neuron_bench) 

            # Generate the string which connects the synapses to this neuron
            connection_string = generate_neuron_connection_verilog(indices, input_bitwidth)
            wire_name = f"{module_name}_wire" 
            connection_line = f"wire [{len(indices)* input_bitwidth-1}:0] {wire_name} = {{{connection_string}}}; \n" 
            inst_line = f"{module_name} {module_name}_inst (.M0({wire_name}), .M1(M1[{output_offset+output_bitwidth-1}:{output_offset}])); \n\n"
            layer_contents += connection_line + inst_line
            output_offset += output_bitwidth
        layer_contents += 'endmodule'

        with open(f'{directory}/{module_prefix}.v', 'w') as f: 
            f.write(layer_contents)
        
        return total_input_bits, total_output_bits


    def gen_neuron_verilog(self, index, module_name): 
        indices, input_perm_matrix, float_output_states, bin_output_states = self.neuron_truth_tables[index] 
        _, input_bitwidth = self.input_quant.get_scale_factor_bits()
        _, output_bitwidth = self.output_quant.get_scale_factor_bits()
        cat_input_bitwidth = len(indices) * input_bitwidth
        lut_string = '' 
        num_entries = input_perm_matrix.shape[0] 
        
        for i in range(num_entries): 
            entry_string = '' 
            for idx in range(len(indices)): 
                val = input_perm_matrix[i, idx]
                entry_string += self.input_quant.get_bin_str(val) 
            
            res_str = self.output_quant.get_bin_str(bin_output_states[i])
            lut_string += f"\t\t\t{int(cat_input_bitwidth)}'b{entry_string}:M1r = {int(output_bitwidth)}'b{res_str};\n"
        return generate_lut_verilog(module_name, int(cat_input_bitwidth), int(output_bitwidth), lut_string)
    

    def gen_neuron_bench(self, index, module_name): 
        indices, input_perm_matrix, float_output_states, bin_output_states = self.neuron_truth_tables[index] 
        _, input_bitwidth = self.input_quant.get_scale_factor_bits()
        _, output_bitwidth = self.output_quant.get_scale_factor_bits()
        cat_input_bitwidth = len(indices) * input_bitwidth
        lut_string = '' 
        num_entries = input_perm_matrix.shape[0]

        # sorting the input perm matrix to match the bench format 
        input_state_space_bin_str = list(map(lambda y: list(map(lambda z:self.input_quant.get_bin_str(z), y)), input_perm_matrix))
        sorted_bin_output_states = sort_to_bench(input_state_space_bin_str, bin_output_states) 

        # Generate the LUT for each output: 
        for i in range(int(output_bitwidth)): 
            lut_string += f"M1[{i}]             =LUT 0x"
            output_bin_str = reduce(lambda b,c: b+c, map(lambda a:self.output_quant.get_bin_str(a)[int(output_bitwidth)-1-i], sorted_bin_output_states))
            lut_hex_string = f"int{int(output_bin_str, 2):0{int(num_entries/4)}x} "
            lut_string += lut_hex_string 
            lut_string += generate_lut_input_string(int(cat_input_bitwidth))
        
        return generate_lut_bench(int(cat_input_bitwidth), int(output_bitwidth), lut_string) 
    

    def lut_inference(self): 
        self.is_lut_inference = True
        self.input_quant.bin_output()
        self.output_quant.bin_output()


    def neq_inference(self): 
        self.is_lut_inference = False
        self.input_quant.float_output()
        self.output_quant.float_output()


    def table_lookup(self, connected_input: Tensor, input_perm_matrix: Tensor, bin_output_states: Tensor) -> Tensor: 
        fan_in_size = connected_input.shape[1]
        ci_bcast = connected_input.unsqueeze(2) 
        pm_bcast = input_perm_matrix.t().unsqueeze(0) 
        eq = (ci_bcast == pm_bcast).sum(dim=1) == fan_in_size 
        matches = eq.sum(dim=1)
        if not (matches == torch.ones_like(matches, dtype = matches.dtype)).all(): 
            raise Exception(f'One or more vectors in the input is not in the possible input state space')
        indices = torch.argmax(eq.type(torch.int64), dim=1) 
        return bin_output_states[indices]
    

    def lut_forward(self, x: Tensor) -> Tensor: 
        if self.apply_input_quant: 
            x = self.input_quant(x)
       
        y = torch.zeros((x.shape[0], self.out_features))
        if self.first_linear: 
            x = x.view(x.size(0), -1)
        for i in range(self.out_features):       
            print(f'pefrorming lut_forward for out_feature {i}')          
            indices, input_perm_matrix, float_output_states, bin_output_states = self.neuron_truth_tables[i]
            connected_input = x[:,indices]
            y[:,i] = self.table_lookup(connected_input, input_perm_matrix, bin_output_states)
        
        return y
    

    def forward(self, x: Tensor) -> Tensor:
        if self.is_lut_inference: 
            x = self.lut_forward(x)
            
        else: 
            if self.apply_input_quant:
                x = self.input_quant(x)

            if self.first_linear:
                x = x.view(x.size(0), -1)

            x = self.fc(x)
            if self.apply_output_quant and self.output_quant is not None:
                x = self.output_quant(x)
        return x
    

    def calculate_truth_tables(self): 
        with torch.no_grad(): 
            mask = self.fc.mask() 
            input_state_space = list()
            bin_state_space = list() 

            if self.first_linear: 
                self.in_features = self.reshaped_in_features

            for m in range(self.in_features): 
                neuron_state_space = self.input_quant.get_state_space()
                bin_space = self.input_quant.get_bin_state_space() 
                
                input_state_space.append(neuron_state_space)
                bin_state_space.append(bin_space)
            print(f'The state space has been generated for {len(input_state_space)} neurons') 

            neuron_truth_tables = list() 
            for n in range(self.out_features): 
                print('processing out feature no. :', n)
                input_mask = mask[n,:]
                fan_in = torch.sum(input_mask) 
                indices = fetch_mask_indices(input_mask)

                # Retrieve the possible state space of the current neuron
                connected_state_space = [input_state_space[i] for i in indices]
                bin_connected_state_space = [bin_state_space[i] for i in indices] 

                # Generate a matrix containing all possible input states 
                input_permutation_matrix = generate_permutation_matrix(connected_state_space) 
                bin_input_permutation_matrix = generate_permutation_matrix(bin_connected_state_space) 
                num_permutations = input_permutation_matrix.shape[0]
                padded_perm_matrix = torch.zeros((num_permutations, self.in_features))
                padded_perm_matrix[:, indices] = input_permutation_matrix
                
                apply_input_quant, apply_output_quant = self.apply_input_quant, self.apply_output_quant
                self.apply_input_quant, self.apply_output_quant = False, False
                is_bin_output = self.output_quant.is_bin_output 
                self.output_quant.float_output() 
                output_states = self.output_quant(self.forward(padded_perm_matrix))[:, n] # Calculate float for the current input
                self.output_quant.bin_output()
                bin_output_states = self.output_quant(self.forward(padded_perm_matrix))[:, n] # Calculate bin for the current input 
                self.output_quant.is_bin_output = is_bin_output 
                self.apply_input_quant, self.apply_output_quant = apply_input_quant, apply_output_quant
                # append the necessary parameter to the truth tables list
                neuron_truth_tables.append((indices, bin_input_permutation_matrix, output_states, bin_output_states))
            self.neuron_truth_tables = neuron_truth_tables
    

# This vlass is a customized forward function to be applies for the truth tables forward functions after flattening the inputs
class flattenedsparseconv(qnn.QuantConv1d):
    def __init__(self, in_channels, out_channels, kernel_size, padding=1):
        super(flattenedsparseconv, self).__init__(in_channels, out_channels, kernel_size, padding=1)

    def forward(self, x):
        # Flatten the weights to a 1D tensor
        flattened_weights = self.weight.reshape(self.weight.size(0), self.weight.size(1) * self.weight.size(2)).t()   
        output = torch.matmul(x, flattened_weights)
        
        return output
    

class SparseConv1d(qnn.QuantConv1d):
    def __init__(self, in_channels, out_channels, mask:nn.Module, kernel_size=3, padding=1, bias=False) -> None:
        super(SparseConv1d, self).__init__(in_channels, out_channels, kernel_size, padding=padding, bias=bias)
        self.mask = mask
    def forward(self, input) -> Tensor:
        masked_weights = self.weight * self.mask()
        output = F.conv1d(input, masked_weights, self.bias, padding=self.padding, stride=1)
        return output 



# Applying the customized convolutional forward function defined above along with the input and/or output quantization function from the brevitas module 
# TODO: this class is to be further customized to allow for more functionality when the hardware is intoduced 
class SparseConv1dNeq(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, seq_length, input_quant, output_quant, mask, apply_input_quant=True, apply_output_quant=True, padding=1) -> None:
        super(SparseConv1dNeq, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self.seq_length = seq_length
        self.input_quant = input_quant
        self.padding = padding
        self.conv = SparseConv1d(in_channels, out_channels, mask, kernel_size, padding=padding, bias=False)
        self.flatconv = flattenedsparseconv(in_channels, out_channels, kernel_size, padding)
        self.output_quant = output_quant
        self.is_lut_inference = False
        self.apply_input_quant = apply_input_quant
        self.apply_output_quant = apply_output_quant 
    

    def lut_cost(self):  # Is this valid for 1D-CNN? 
        """
        Approximate how many 6:1 LUTs are needed to implement this layer using 
        LUTCost() as defined in LogicNets paper FPL'20:
            LUTCost(X, Y) = (Y / 3) * (2^(X - 4) - (-1)^X)
        where:
        * X: input fanin bits
        * Y: output bits 
        LUTCost() estimates how many LUTs are needed to implement 1 neuron, so 
        we then multiply LUTCost() by the number of neurons to get the total 
        number of LUTs needed.
        NOTE: This function (over)estimates how many 6:1 LUTs are needed to implement
        this layer b/c it assumes every neuron is connected to the next layer 
        since we do not have the next layer's sparsity information.
        """
        # Compute LUTCost of 1 neuron
        _, input_bitwidth = self.input_quant.get_scale_factor_bits()
        _, output_bitwidth = self.output_quant.get_scale_factor_bits()
        input_bitwidth, output_bitwidth = int(input_bitwidth), int(output_bitwidth)
        x = input_bitwidth * self.conv.mask.fan_in # neuron input fanin
        y = output_bitwidth 
        neuron_lut_cost = (y / 3) * ((2 ** (x - 4)) - ((-1) ** x))
        # Compute total LUTCost
        return self.out_channels * neuron_lut_cost
    

    def gen_layer_verilog(self, module_prefix, directory, generate_bench: bool = True): 

        _, input_bitwidth = self.input_quant.get_scale_factor_bits()
        _, output_bitwidth = self.output_quant.get_scale_factor_bits()
        input_bitwidth, output_bitwidth = int(input_bitwidth), int(output_bitwidth)
        total_input_bits = self.in_channels * input_bitwidth * self.kernel_size
        total_output_bits = self.out_channels * output_bitwidth

        # The line below taked the module_prefix which is a (layer number or name) and print the input and output bitwidth
        layer_contents = f"module {module_prefix} (input [{total_input_bits-1}:0] M0, output[{total_output_bits-1}:0] M1); \n\n"
        output_offset = 0 

        for index in range(self.out_channels): 
            module_name = f"{module_prefix}_N{index}" 
            indices, state_space_indices, _, _, _ = self.out_channel_truth_table[index]
            
            neuron_verilog = self.gen_neuron_verilog(index, module_name) 

            with open(f"{directory}/{module_name}.v", "w") as f: 
                f.write(neuron_verilog)
            
            if generate_bench: 
                # Generate the contents of the neuron verilog
                neuron_bench = self.gen_neuron_bench(index, module_name)
                with open(f"{directory}/{module_name}.bench", "w") as f: 
                    f.write(neuron_bench) 

            # Generate the string which connects the synapses to this neuron
            for pos in range(self.seq_length):  # loop over sequence positions
            # Adjust the input connections to reflect the sliding window
                connection_string = generate_sliding_window_connection_verilog(state_space_indices, pos, input_bitwidth, self.kernel_size)
                wire_name = f"{module_name}_pos{pos}_wire" 
                connection_line = f"wire [{len(indices) * self.kernel_size * input_bitwidth - 1}:0] {wire_name} = {{{connection_string}}}; \n" 
                inst_line = f"{module_name} {module_name}_inst_pos{pos} (.M0({wire_name}), .M1(M1[{output_offset + output_bitwidth - 1}:{output_offset}])); \n\n"
                layer_contents += connection_line + inst_line
        
            output_offset += output_bitwidth

        layer_contents += 'endmodule'

        with open(f'{directory}/{module_prefix}.v', 'w') as f: 
            f.write(layer_contents)
        
        return total_input_bits, total_output_bits
    
    def generate_sliding_window_connection_verilog(indices, position, input_bitwidth, kernel_size):
        """
        This function generates the Verilog wiring for a sliding window of inputs for convolution.
        Each window has `kernel_size` inputs and reuses the same filter weights.
        """
        connection_string = ''
        for i in range(len(indices)):
            for k in range(kernel_size):
                start_bit = (position + k) * input_bitwidth
                end_bit = start_bit + input_bitwidth - 1
                connection_string += f"M0[{end_bit}:{start_bit}], "

        return connection_string.rstrip(", ")  # Remove the last comma

    def gen_neuron_verilog(self, index, module_name): 
        indices, state_space_indices, input_perm_matrix, float_output_states, bin_output_states = self.out_channel_truth_table[index] 
        _, input_bitwidth = self.input_quant.get_scale_factor_bits()
        _, output_bitwidth = self.output_quant.get_scale_factor_bits()
        cat_input_bitwidth = len(state_space_indices) * input_bitwidth
        lut_string = '' 
        num_entries = input_perm_matrix.shape[0] 
        
        for i in range(num_entries): 
            entry_string = '' 
            for idx in range(len(state_space_indices)): 
                val = input_perm_matrix[i, idx]
                entry_string += self.input_quant.get_bin_str(val) 
            
            res_str = self.output_quant.get_bin_str(bin_output_states[i])
            lut_string += f"\t\t\t{int(cat_input_bitwidth)}'b{entry_string}:M1r = {int(output_bitwidth)}'b{res_str};\n"

        return generate_lut_verilog(module_name, int(cat_input_bitwidth), int(output_bitwidth), lut_string)
    

    def gen_neuron_bench(self, index, module_name):
        for i in range(self.seq_length): 
            indices, state_space_indices, input_perm_matrix, float_output_states, bin_output_states = self.out_channel_truth_table[index] 
            _, input_bitwidth = self.input_quant.get_scale_factor_bits()
            _, output_bitwidth = self.output_quant.get_scale_factor_bits()
            cat_input_bitwidth = len(indices) * self.kernel_size * input_bitwidth
            lut_string = '' 
            num_entries = input_perm_matrix.shape[0]

            # sorting the input perm matrix to match the bench format 
            input_state_space_bin_str = list(map(lambda y: list(map(lambda z:self.input_quant.get_bin_str(z), y)), input_perm_matrix))
            sorted_bin_output_states = sort_to_bench(input_state_space_bin_str, bin_output_states) 

            # Generate the LUT for each output: 
            for i in range(int(output_bitwidth)): 
                lut_string += f"M1[{i}]             =LUT 0x"
                output_bin_str = reduce(lambda b,c: b+c, map(lambda a:self.output_quant.get_bin_str(a)[int(output_bitwidth)-1-i], sorted_bin_output_states))
                lut_hex_string = f"int{int(output_bin_str, 2):0{int(num_entries/4)}x} "
                lut_string += lut_hex_string 
                lut_string += generate_lut_input_string(int(cat_input_bitwidth))
            
            return generate_lut_bench(int(cat_input_bitwidth), int(output_bitwidth), lut_string)
    

    def lut_inference(self): 
        self.is_lut_inference = True
        self.input_quant.bin_output()
        self.output_quant.bin_output()


    def neq_inference(self): 
        self.is_lut_inference = False
        self.input_quant.float_output()
        self.output_quant.float_output()


    def table_lookup(self, connected_input: Tensor, input_perm_matrix: Tensor, bin_output_states: Tensor) -> Tensor:
        batch_size, active_channels, sequence_length = connected_input.shape
        fan_in_size = input_perm_matrix.shape[1]
        kernel_size = self.kernel_size
        bin_output_states = bin_output_states.squeeze(0)  #NOTE: Is this necessary 

        # Add padding to ensure output has the same sequence length
        padded_input = torch.nn.functional.pad(connected_input, (1, 1))
        acc_outputs = torch.zeros(batch_size, sequence_length)


        for i in range(sequence_length):
            # Extract the current window
            window = padded_input[:, :, i:i + kernel_size]  # Shape: [batch_size, active_channels, kernel_size]
            window_reshaped = window.reshape(window.size(0), window.size(1) * window.size(2))  # reshaped to [batch , (in_ch X kernel size)]
            
            # Broadcast the input and perm matrix
            ci_bcast = window_reshaped.unsqueeze(2)  # (Batch, Flattened input, 1)
            pm_bcast = input_perm_matrix.t().unsqueeze(0)  # (1, Permutations, Flattened input)

            # Perform element-wise comparison and match
            eq = (ci_bcast == pm_bcast).sum(dim=1) == fan_in_size  # Shape: [batch_size, permutations]
            matches = eq.sum(dim=1)  # Shape: [batch_size]
            if not (matches == torch.ones_like(matches, dtype=matches.dtype)).all():
                raise Exception(f"One or more vectors in the input is not in the possible input state space")
        
            # Find the indices of the matching input states
            indices = torch.argmax(eq.type(torch.int64), dim=1) # Shape: [batch_size]   .type(torch.int64)
            output_states = bin_output_states[indices]
            acc_outputs[:, i] = output_states
        
        return acc_outputs

    
    
    def lut_forward(self, x: torch.Tensor) -> torch.Tensor: 
        if self.apply_input_quant: 
            x = self.input_quant(x) 

        batch_size, _, sequence_length = x.shape
        y = torch.zeros(batch_size, self.out_channels, sequence_length)  

        # Performing table_lookup for each neuron output
        for i in range (self.out_channels): 
            print(f'processing out channel {i} for lut forward') 
            indices, state_space_indices, input_perm_matrix, float_output_states, bin_output_states = self.out_channel_truth_table[i]
            connected_input = x[:, indices, :]
            y[:, i, :] = self.table_lookup(connected_input, input_perm_matrix, bin_output_states)

        return y


    def forward(self, x: Tensor) -> Tensor:
        if self.is_lut_inference:
            x = self.lut_forward(x)

        else:     
            if self.apply_input_quant:
                x = self.input_quant(x)

            x = self.conv(x)
            if self.apply_output_quant:
                x = self.output_quant(x)
        return x
    

    def truth_table_forward(self, x: Tensor) -> Tensor: 
        input = x.t()         
        x = self.flatconv(input) 
        if self.apply_output_quant: 
            self.output_quant(x) 

        return x
    
    
    def calculate_truth_tables_conv2(self): 
        with torch.no_grad(): 
            mask = self.conv.mask() 
            channel_state_space = []   
            bin_channel_state_space = []
            for i in range(self.in_channels): 
                for k in range(self.kernel_size):
                    sample_state_space = self.input_quant.get_state_space()
                    bin_sample_state_space = self.input_quant.get_bin_state_space()

                    channel_state_space.append(sample_state_space)
                    bin_channel_state_space.append(bin_sample_state_space) 

            print('the state space has been generated for: %s channels and kernels combinations' %(len(channel_state_space)))

            out_channel_truth_table = []
            for out_c in range(self.out_channels): 
                input_mask = mask[out_c, :, :] 
                flattened_input = input_mask.flatten() 
                fan_in = torch.sum(flattened_input) # resembling the number of total elements fed into the outchannel
                state_space_indices = fetch_mask_indices(flattened_input)
                indices = fetch_mask_indices_edited(input_mask)

                # Get the channel state space for the specific channel index
                connected_state_space = [channel_state_space[i] for i in state_space_indices] # was state_space_indices
                bin_connected_state_space = [bin_channel_state_space[i] for i in state_space_indices]

                permutation_matrix = generate_permutation_matrix(connected_state_space)
                bin_permutation_matrix = generate_permutation_matrix(bin_connected_state_space)
                num_permutations  = permutation_matrix.shape[0]
                
                padded_permutation_matrix = torch.zeros((self.in_channels, self.kernel_size, num_permutations))
                reshaped_padded = padded_permutation_matrix.view(padded_permutation_matrix.size(0) * padded_permutation_matrix.size(1), -1) # reshaped to broadcast 
                reshaped_padded[state_space_indices, :] = permutation_matrix.t() 
                
                apply_input_quant, apply_output_quant = self.apply_input_quant, self.apply_output_quant
                self.apply_input_quant, self.apply_output_quant = False, False
                is_bin_output = self.output_quant.is_bin_output
                self.output_quant.float_output()
                output_state = self.output_quant(self.truth_table_forward(reshaped_padded))[:, out_c]
                self.output_quant.bin_output()
                bin_output_state = self.output_quant(self.truth_table_forward(reshaped_padded))[:, out_c]
                self.output_quant.is_bin_output = is_bin_output
                self.apply_input_quant, self.apply_output_quant = apply_input_quant, apply_output_quant
                # appending the necessary parameters into the channel truth table
                out_channel_truth_table.append((indices, state_space_indices, bin_permutation_matrix, output_state, bin_output_state))
                print('The truth table for the output channel %s has been generated' %out_c)
                
        self.out_channel_truth_table = out_channel_truth_table


# The classes below are defining the Random-Fixed-Sparsity mask for both layers type 
# MLP layers mask
class DenseMask2D(nn.Module):
    def __init__(self, in_features: int, out_features: int) -> None:
        super(DenseMask2D, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.mask = Parameter(torch.Tensor(self.out_features, self.in_features), requires_grad=False)
        self.reset_parameters()

    def reset_parameters(self) -> None:
        init.constant_(self.mask, 1.0)

    def forward(self):
        return self.mask



class RandomFixedSparsityMask2D(nn.Module):
    def __init__(self, in_features: int, out_features: int, fan_in: int) -> None:
        super(RandomFixedSparsityMask2D, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.fan_in = fan_in
        self.mask = Parameter(torch.Tensor(self.out_features, self.in_features), requires_grad=False)
        self.reset_parameters()

    def reset_parameters(self) -> None:
        init.constant_(self.mask, 0.0)
        for i in range(self.out_features):
            x = torch.randperm(self.in_features)[:self.fan_in]  
            self.mask[i][x] = 1

    def forward(self):
        return self.mask
    
    def print_mask_size(self):
        print(f"Mask size in RandomFixedSparsityMask2D: {self.mask.size()}")
    
    def count_zero_elements(self):
        zero_count = torch.sum(self.mask == 0).item()
        print(f"Number of zero elements in the mask: {zero_count}")
        return zero_count

    def count_total_elements(self):
        total_elements = self.mask.numel()
        print(f"Total number of elements in the mask: {total_elements}")
        return total_elements
    

# CNN layers masks 
class Conv1DMask(nn.Module):
    def __init__(self, out_channels: int, in_channels: int, kernel_size: int) -> None:
        super(Conv1DMask, self).__init__()
        self.out_channels = out_channels
        self.in_channels = in_channels
        self.kernel_size = kernel_size
        self.mask = Parameter(torch.Tensor(self.out_channels, self.in_channels, kernel_size), requires_grad=False)
        self.reset_parameters()

    def reset_parameters(self) -> None:
        init.constant_(self.mask, 1.0)

    def forward(self):
        return self.mask

    
class RandomFixedSparsityConv1DMask(nn.Module):
    def __init__(self, out_channels: int, in_channels: int, kernel_size: int, fan_in: int) -> None:
        super(RandomFixedSparsityConv1DMask, self).__init__()
        self.out_channels = out_channels
        self.in_channels = in_channels
        self.kernel_size = kernel_size
        self.fan_in = fan_in
        self.mask = Parameter(torch.Tensor(self.out_channels, self.in_channels, self.kernel_size), requires_grad=False)
        self.reset_parameters()

    def reset_parameters(self) -> None:
        init.constant_(self.mask, 0.0)
        for i in range(self.out_channels):
            selected_in_channels = torch.randperm(self.in_channels)[:self.fan_in]
            self.mask[i, selected_in_channels, :] = 1.0

    def forward(self):
        return self.mask

    def print_mask_size(self):
        print(f"Mask size in RandomFixedSparsityConv1DMask: {self.mask.size()}")
    
    def count_zero_elements(self):
        zero_count = torch.sum(self.mask == 0).item()
        print(f"Number of zero elements in the mask: {zero_count}")
        return zero_count

    def count_total_elements(self):
        total_elements = self.mask.numel()
        print(f"Total number of elements in the mask: {total_elements}")
        return total_elements