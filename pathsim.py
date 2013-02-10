import stem.descriptor.reader as sdr
import datetime
import os
import os.path
import stem.descriptor as sd
import stem.descriptor.networkstatus as sdn
import stem
import random
import sys
import collections

def timestamp(t):
    """Returns UNIX timestamp"""
    td = t - datetime.datetime(1970, 1, 1)
    ts = td.days*24*60*60 + td.seconds
    return ts

def process_consensuses(descriptor_dir, consensus_dir, out_dir):
    """For every input consensus, finds the descriptors published most recently before that consensus for every contained relay, and outputs that list of descriptors."""
    # read all descriptors into memory
    descriptors = {}
    num_descriptors = 0    
    num_relays = 0
    
    def skip_listener(path, event):
        print('ERROR [{0}]: {1}'.format(path, event))
    
    with sdr.DescriptorReader(descriptor_dir, validate=False) as reader:
        reader.register_skip_listener(skip_listener)
        for desc in reader:
            if (num_descriptors % 1000 == 0):
                print(num_descriptors)
            num_descriptors += 1
            if (desc.fingerprint not in descriptors):
                descriptors[desc.fingerprint] = {}
                num_relays += 1
            descriptors[desc.fingerprint][timestamp(desc.published)] = desc
#            print('Adding {0}:{1}:{2}'.format(desc.nickname,desc.fingerprint,\
#                timestamp(desc.published)))
    print('#descriptors: {0}; #relays:{1}'.format(num_descriptors,num_relays)) 

    # go through consensuses, output most recent descriptors for relays
    num_consensuses = 0
    for dirpath, dirnames, filenames in os.walk(consensus_dir):
        for filename in filenames:
            if (filename[0] != '.'):
                print(filename)
                with open(os.path.join(dirpath,filename), 'r') as cons_f:
                    relays = []
                    cons_t = None
                    for r_stat in sd.parse_file(cons_f, validate=False):
                        cons_t = r_stat.document.valid_after
                        # find descriptor published just before consensus time
                        pub_t = timestamp(r_stat.published)
                        desc_t = 0
                        # get all descriptors with this fingerprint
                        if (r_stat.fingerprint in descriptors):
                            for t in descriptors[r_stat.fingerprint].keys():
                                if (t <= pub_t) and (t >= desc_t):
                                    desc_t = t
                        if (desc_t == 0):
                            print(\
                            'Descriptor not found for {0} :\{1}:{2}'.format(\
                                r_stat.nickname,r_stat.fingerprint,pub_t))
                        else:
                            relays.append(\
                                descriptors[r_stat.fingerprint][desc_t])
                    # output all discovered descriptors
                    if cons_t:
                        outpath = os.path.join(out_dir,\
                            cons_t.strftime('%Y-%m-%d-%H-%M-%S-descriptor'))
                        f = open(outpath,'w')
                        # annotation needed for stem parser to work correctly
                        f.write('@type server-descriptor 1.0\n')                    
                        for relay in relays:
                            f.write(str(relay))
                            f.write('\n')
                        f.close()                
                    num_consensuses += 1
    print('# consensuses: {0}'.format(num_consensuses))

def get_bw_weight(flags, position, bw_weights):
    """Returns weight to apply to relay's bandwidth for given position.
        flags: list of stem.Flag values for relay from a consensus
        position: position for which to find selection weight,
             one of 'g' for guard, 'm' for middle, and 'e' for exit
        bw_weights: bandwidth_weights from NetworkStatusDocumentV3 consensus
    """
    
    if (position == 'g'):
        if (stem.Flag.GUARD in flags) and (stem.Flag.EXIT in flags):
            return bw_weights['Wgd']
        elif (stem.Flag.GUARD in flags):
            return bw_weights['Wgg']
        elif (stem.Flag.EXIT not in flags):
            return bw_weights['Wgm']
        else:
            raise ValueError('Wge weight does not exist.')
    elif (position == 'm'):
        if (stem.Flag.GUARD in flags) and (stem.Flag.EXIT in flags):
            return bw_weights['Wmd']
        elif (stem.Flag.GUARD in flags):
            return bw_weights['Wmg']
        elif (stem.Flag.EXIT in flags):
            return bw_weights['Wme']
        else:
            return bw_weights['Wmm']
    elif (position == 'e'):
        if (stem.Flag.GUARD in flags) and (stem.Flag.EXIT in flags):
            return bw_weights['Wed']
        elif (stem.Flag.GUARD in flags):
            return bw_weights['Weg']
        elif (stem.Flag.EXIT in flags):
            return bw_weights['Wee']
        else:
            return bw_weights['Wem']    
    else:
        raise ValueError('get_weight does not support position {0}.'.format(
            position))
            
def select_weighted_node(weighted_nodes):
    """Takes (node,weight) pairs, where the weights sum to 1.
    Select node with probability weight."""
    r = random.random()
    cum_prob = 0
    for node, weight in weighted_nodes:
        if (r <= cum_prob + weight):
            return node
        else:
            cum_prob += weight
    raise ValueError('Weights must sum to 1.')   
    
def can_exit_to_port(descriptor, port):
    """Returns if there is *some* ip that relay will exit to on port."""             
    can_exit = None
    for rule in descriptor.exit_policy:
        if (port >= rule.min_port) and\
            (port <= rule.max_port) and\
            rule.is_accept and (can_exit==None):
            can_exit = True
        elif (port >= rule.min_port) and\
            (port <= rule.max_port) and\
            (not rule.is_accept) and\
            rule.is_address_wildcard() and (can_exit==None):
            can_exit = False
    if (can_exit == None): # default accept if no rule matches
        can_exit = True
    return can_exit                 
    
def filter_exits(cons_rel_stats, descriptors):
    """Applies basic exit filter to given relays."""
    exits = []
    for fprint in cons_rel_stats:
        rel_stat = cons_rel_stats[fprint] 
        desc = descriptors[fprint]   
        if (stem.Flag.BADEXIT not in rel_stat.flags) and\
            (stem.Flag.RUNNING in rel_stat.flags) and\
            (stem.Flag.VALID in rel_stat.flags) and\
            (not desc.hibernating):
            exits.append(fprint)
    return exits
    
def get_position_weights(nodes, cons_rel_stats, position, bw_weights,\
    bwweightscale):
    """Computes the consensus "bandwidth" weighted by position weights."""
    weights = {}
    for node in nodes:
        bw = float(cons_rel_stats[node].bandwidth)
        weight = float(get_bw_weight(cons_rel_stats[node].flags,\
            position,bw_weights)) / float(bwweightscale)
        weights[node] = bw * weight
    return weights        

def get_weighted_exits(bw_weights, bwweightscale, cons_rel_stats,\
    descriptors, fast, stable, internal, ip, port, potential_exits=None,\
    exit_weights=None, middle_weights=None):
    """Returns list of fingerprints for potential exits along with
    selection weights for use in a circuit with the indicated properties.
        exit_list: (Optional) precomputed list of potential exits
        exit_weights: (Optional) precomputed list of relay weights as exit
        middle_weights: (Optional) precomputed list of relay weights as middle        
    """    
    if (port == None) and (not internal):
        raise ValueError('get_weighted_exits() needs a port.')            
        
    exits = []

    # apply basic exit filter if not already done
    if (potential_exits == None):
        potential_exits = filter_exits(cons_rel_stats, descriptors)    
    
    for fprint in potential_exits:
        rel_stat = cons_rel_stats[fprint]     
        desc = descriptors[fprint]
        if ((not fast) or (stem.Flag.FAST in rel_stat.flags)) and\
            ((not stable) or (stem.Flag.STABLE in rel_stat.flags)):
            if (internal):
                # In an "internal" circuit final node is chosen just like a
                # middle node (ignoring its exit policy).
                exits.append(fprint)
            else:
                if (ip != None) and (desc.exit_policy.can_exit_to(ip, port)):
                    exits.append(fprint)
                elif (can_exit_to_port(desc, port)):
                    exits.append(fprint)
                    
    # create weights
    weights = None
    if (internal):
        if (middle_weights == None):
            weights = get_position_weights(exits, cons_rel_stats, 'm',\
                        bw_weights, bwweightscale)
        else:
            weights = middle_weights
    else:
        if (exit_weights == None):
            weights = get_position_weights(exits, cons_rel_stats, 'e',\
                bw_weights, bwweightscale)
        else:
            weights = exit_weights
    total_weight = 0
    for exit in exits:
        total_weight += weights[exit]
    weighted_exits = []
    for exit in exits:
        weighted_exits.append((exit,weights[exit]/total_weight))
        
    return weighted_exits
    
def in_same_family(descriptors, node1, node2):
    """Takes list of descriptors and two node fingerprints,
    checks if nodes list each other as in the same family."""
    
    desc1 = descriptors[node1]
    desc2 = descriptors[node2]
    family1 = desc1.family
    family2 = desc2.family
    node1_lists_node2 = False
    for member in family1:
        if (member == ('$'+desc2.fingerprint)) or\
            (member == desc2.nickname):
            node1_lists_node2 = True
    node2_lists_node1 = False
    for member in family2:
        if (member == ('$'+desc1.fingerprint)) or\
            (member == desc1.nickname):
            node2_lists_node1 = True
    return (node1_lists_node2 and node2_lists_node1)
    
def in_same_16_subnet(address1, address2):
    """Takes IPv4 addresses as strings and checks if the first two bytes
    are equal."""
    address1_list = address1.split('.')
    address2_list = address2.split('.')
    
    # do some address format checking
    if (len(address1_list) == 4) and\
        (len(address2_list) == 4):
        for substr in address1_list:
            if (not substr.isdigit()):
                raise ValueError(\
                    'in_same_16_subset() needs IPv4 address strings')
        for substr in address2_list:
            if (not substr.isdigit()):
                raise ValueError(\
                    'in_same_16_subset() needs IPv4 address strings')

    return (address1_list[0] == address2_list[0]) and\
        (address1_list[1] == address2_list[1])

def filter_middles(cons_rel_stats, descriptors):
    """Applies basic middle filter to relays."""
    middles = []
    for fprint in cons_rel_stats:
        rel_stat = cons_rel_stats[fprint]
        desc = descriptors[fprint]
        if (stem.Flag.RUNNING in rel_stat.flags) and\
            (not desc.hibernating):
            middles.append(fprint)
    return middles
    
def get_weighted_middles(bw_weights, bwweightscale, cons_rel_stats,\
    descriptors, fast, stable, exit_node, guard_node, potential_middles=None,\
    middle_weights=None):
    """Returns list of fingerprints for potential middle nodes along with
    selection weights for use in a circuit with the indicated properties."""
    
    if (potential_middles == None):
        potential_middles = filter_middles(cons_rel_stats, descriptors)
    
    # filter out some nodes with zero selection probability
    # Note that we intentionally allow non-Valid routers for middle
    # as per path-spec.txt default config
    middles = []
    for fprint in potential_middles:
        rel_stat = cons_rel_stats[fprint]
        desc = descriptors[fprint]
        if ((not fast) or (stem.Flag.FAST in rel_stat.flags)) and\
            ((not stable) or (stem.Flag.STABLE in rel_stat.flags)) and\
            (exit_node != fprint) and\
            (not in_same_family(descriptors, exit_node, fprint)) and\
            (not in_same_16_subnet(descriptors[exit_node].address,\
                descriptors[fprint].address)) and\
            (guard_node != fprint) and\
            (not in_same_family(descriptors, guard_node, fprint)) and\
            (not in_same_16_subnet(descriptors[guard_node].address,\
                descriptors[fprint].address)):
            middles.append(fprint)

    # create weights
    weights = None   
    if (middle_weights == None):
        weights = get_position_weights(middles, cons_rel_stats, 'm',\
            bw_weights, bwweightscale)
    else:
        weights = middle_weights
        
    total_weight = 0
    for middle in middles:
        total_weight += weights[middle]

    weighted_middles = []
    for middle in middles:
        weighted_middles.append((middle,weights[middle]/total_weight))

    return weighted_middles

def guard_filter_for_circ(guard, cons_rel_stats, descriptors, fast,\
    stable, exit, guards):
    """Returns if guard is usable for circuit."""
    #  - liveness (given by entry_is_live() call in choose_random_entry_impl())
    #       - not bad_since
    #       - has descriptor (although should be ensured by create_circuits()
    #  - fast/stable
    #  - not same as exit
    #  - not in exit family
    #  - not in exit /16
    # note that Valid flag not checked
    # also note that hibernate status not checked
    
    if (guards[guard]['bad_since'] == None):
        if (guard in cons_rel_stats) and\
            (guard in descriptors):
            rel_stat = cons_rel_stats[guard]
            return ((not fast) or (stem.Flag.FAST in rel_stat.flags)) and\
                ((not stable) or (stem.Flag.STABLE in rel_stat.flags)) and\
                (exit != guard) and\
                (not in_same_family(descriptors, exit, guard)) and\
                (not in_same_16_subnet(descriptors[exit].address,\
                           descriptors[guard].address))
        else:
            raise ValueError('Guard {0} not present in consensus or\ descriptors but wasn\'t marked bad.'.format(guard))
    else:
        return False

def get_new_guard(bw_weights, bwweightscale, cons_rel_stats, descriptors,\
    guards):
    """Selects a new guard that doesn't conflict with the existing list.
    Note: will raise ValueError if no suitable guard is found."""
    # - doesn't conflict with current guards
    # - running
    # - valid
    # - need guard    
    # - need descriptor, though should be ensured already by create_circuits()
    # - not single hop relay
    # Note that hibernation is not considered.
    # follows add_an_entry_guard(NULL,0,0,for_directory) call which appears
    # in pick_entry_guards() and more directly in choose_random_entry_impl()
    potential_guards = []
    for fprint in cons_rel_stats:
        rel_stat = cons_rel_stats[fprint]
        if (stem.Flag.RUNNING in rel_stat.flags) and\
            (stem.Flag.VALID in rel_stat.flags) and\
            (stem.Flag.GUARD in rel_stat.flags) and\
            (fprint in descriptors):            
            guard_conflict = False
            for guard in guards:
                if (guard == fprint) or\
                    (in_same_family(descriptors, guard, fprint)) or\
                    (in_same_16_subnet(descriptors[guard].address,\
                       descriptors[fprint].address)):
                    guard_conflict = True
                    break
            if (not guard_conflict):
                potential_guards.append(fprint)

    # create weights
    weights = []
    for potential_guard in potential_guards:
        bw = float(cons_rel_stats[potential_guard].bandwidth)
        weight = float(get_bw_weight(cons_rel_stats[potential_guard].flags,\
                    'g',bw_weights)) / float(bwweightscale)
        weights.append(bw * weight)

    total_weight = sum(weights)
    weighted_guards = []
    for potential_guard, weight in zip(potential_guards,weights):
        weighted_guards.append((potential_guard,weight/total_weight))
        
    # select new guard according to weight
    return select_weighted_node(weighted_guards)

def get_guards_for_circ(bw_weights, bwweightscale, cons_rel_stats,\
    descriptors,fast, stable, guards, num_guards, min_num_guards, exit,\
    guard_expiration_min, guard_expiration_max, circ_time):
    """Obtains needed number of live guards that will work for circuit.
    Chooses new guards if needed, and *modifies* guard list by adding them."""
    # Get live guards then add new ones until num_guards reached, where live is
    #  - bad_since isn't set
    #  - has descriptor, though create_circuits should ensure descriptor exists
    # Note that node need not have Valid flag to be live. As far as I can tell,
    # a Valid flag is needed to be added to the guard list, but isn't needed 
    # after that point.
    # Note hibernation doesn't affect liveness (dirauths use for Running flag)
    # Rules derived from Tor source: choose_random_entry_impl() in entrynodes.c
    
    # add guards if not enough in list
    if (len(guards) < num_guards):
        # Oddly then only count the number of live ones
        # Slightly depart from Tor code by not considering the circuit's
        # fast or stable flags when finding live guards.
        # Tor uses fixed Stable=False and Fast=True flags when calculating # 
        # live but fixed Stable=Fast=False when adding guards here (weirdly).
        # (as in choose_random_entry_impl() and its pick_entry_guards() call)
        live_guards = filter(lambda x: (guards[x]['bad_since']==None) and\
                                x in descriptors, guards)
        if (len(live_guards) < num_guards):
            for i in range(num_guards - len(live_guards)):
                new_guard = get_new_guard(bw_weights, bwweightscale,\
                    cons_rel_stats, descriptors, guards)
                print('Need guard. Adding {0} [{1}]'.format(\
                    cons_rel_stats[new_guard].nickname, new_guard))
                expiration = random.randint(guard_expiration_min,\
                    guard_expiration_max)
                guards[new_guard] = {'expires':(expiration+\
                    circ_time), 'bad_since':None}

    # check for guards that will work for this circuit
    guards_for_circ = filter(lambda x: guard_filter_for_circ(x,\
        cons_rel_stats, descriptors, fast, stable, exit, guards), guards)
    # add new guards while there aren't enough for this circuit
    # adding is done without reference to the circuit - how Tor does it
    while (len(guards_for_circ) < min_num_guards):
            new_guard = get_new_guard(bw_weights, bwweightscale,\
                cons_rel_stats, descriptors, guards)
            print('Need guard for circuit. Adding {0} [{1}]'.format(\
                cons_rel_stats[new_guard].nickname, new_guard))
            expiration = random.randint(guard_expiration_min,\
                guard_expiration_max)
            guards[new_guard] = {'expires':(expiration+\
                circ_time), 'bad_since':None}
            if (guard_filter_for_circ(new_guard, cons_rel_stats, descriptors,\
                fast, stable, exit, guards)):
                guards_for_circ.append(new_guard)

    # choose first num_guards usable guards
    top_guards_for_circ = guards_for_circ[0:num_guards]
    if (len(top_guards_for_circ) < min_num_guards):
        print('Warning: Only {0} guards for circuit.'.format(\
            len(top_guards_for_circ)))
            
    return top_guards_for_circ


def circuit_covers_port_need(circuit, descriptors, port, need):
    """Returns if circuit satisfies a port need, ignoring the circuit
    time and need expiration."""
    return (can_exit_to_port(descriptors[circuit['path'][-1]], port)) and\
        ((not need['fast']) or (circuit['fast'])) and\
        ((not need['stable']) or (circuit['stable']))
        

def circuit_supports_stream(circuit, stream, long_lived_ports):
    """Returns if stream can run over circuit (which is assumed live)."""

    if (stream['type'] == 'resolve'):
        if (circuit['internal']):
            return True
        else:
            return False
    elif (stream['type'] == 'generic'):
        if (stream['ip'] == None):
            raise ValueError('Stream type generic must have ip.')
        if (stream['port'] == None):
            raise ValueError('Stream type generic must have port.')
    
        desc = circuit['descriptors'][circuit['path'][-1]]
        if (desc.exit_policy.can_exit_to(stream['ip'], stream['port'])) and\
            (not circuit['internal']) and\
            ((circuit['stable']) or\
                (stream['port'] not in long_lived_ports)):
            return True
        else:
            return False
    else:
        raise ValueError('stream type not recognized: {0}'.format(\
            stream['type']))
            

def create_circuit(cons_rel_stats, cons_valid_after, cons_fresh_until,\
    cons_bw_weights, cons_bwweightscale, descriptors, guards,\
    circ_time, circ_fast, circ_stable, circ_internal, circ_ip, circ_port,\
    potential_exits=None, potential_middles=None, exit_weights=None,\
    middle_weights=None):
    """Creates path for requested circuit based on the input consensus
    statuses and descriptors.
    Inputs:
        cons_rel_stats: (dict) relay fingerprint keys and relay status vals
        cons_valid_after: (int) timestamp of valid_after for consensus
        cons_fresh_until: (int) timestamp of fresh_until for consensus
        cons_bw_weights: (dict) bw_weights of consensus
        cons_bwweightscale: (should be float()able) bwweightscale of consensus
        descriptors: (dict) relay fingerprint keys and descriptor vals
        guards: (dict) contains guards of requesting client
        circ_time: (int) timestamp of circuit request
        circ_fast: (bool) all relays should be fast
        circ_stable: (bool) all relays should be stable
        circ_internal: (bool) circuit is for name resolution or hidden service
        circ_ip: (str) IP address of destination (None if not known)
        circ_port: (int) desired TCP port (None if not known)
        potential_exits: (list) relay fingerprints after basic exit filtering
        potential_middles: (list) fingerprints after basic middle filtering
        exit_weights: (dict) consensus weights for exit position
        middle_weights: (dict) consensus weights for middle position
    Output:
        circuit: (dict) a newly created circuit with keys
            'time': (int) seconds from time zero
            'fast': (bool) relays must have Fast flag
            'stable': (bool) relays must have Stable flag
            'internal': (bool) is for DNS or hidden service
            'dirty_time': (int) timestamp of time dirtied, None if clean
            'path': (tuple) list in-order fingerprints for path's nodes
            'cons_rel_stats': (dict) relay stats for active consensus
            'descriptors': (dict) descriptors active during this period
            'covering': (list) ports with needs covered by circuit        
    """
    
    if (circ_time < cons_valid_after) or\
        (circ_time >= cons_fresh_until):
        raise ValueError('consensus not fresh for circ_time in create_circuit')
    
    num_guards = 3
    min_num_guards = 2
    guard_expiration_min = 30*24*3600 # min time until guard removed from list
    guard_expiration_max = 60*24*3600 # max time until guard removed from list
 
    # select exit node
    weighted_exits = get_weighted_exits(cons_bw_weights, 
        cons_bwweightscale, cons_rel_stats, descriptors, circ_fast,
        circ_stable, circ_internal, circ_ip, circ_port, potential_exits,\
        exit_weights, middle_weights)
    exit_node = select_weighted_node(weighted_exits)
    print('Exit node: {0} [{1}]'.format(
        cons_rel_stats[exit_node].nickname,
        cons_rel_stats[exit_node].fingerprint))
    
    # select guard node
    # get first <= num_guards guards suitable for circuit
    circ_guards = get_guards_for_circ(cons_bw_weights,\
        cons_bwweightscale, cons_rel_stats, descriptors,\
        circ_fast, circ_stable, guards, num_guards,\
        min_num_guards, exit_node, guard_expiration_min,\
        guard_expiration_max, circ_time)
    # randomly choose from among those suitable guards
    guard_node = random.choice(circ_guards)
    print('Guard node: {0} [{1}]'.format(
        cons_rel_stats[guard_node].nickname,
        cons_rel_stats[guard_node].fingerprint))
    
    # select middle node
    weighted_middles = get_weighted_middles(cons_bw_weights,
        cons_bwweightscale, cons_rel_stats, descriptors, circ_fast,
        circ_stable, exit_node, guard_node, potential_middles, middle_weights)
    middle_node = select_weighted_node(weighted_middles)                
    print('Middle node: {0} [{1}]'.format(
        cons_rel_stats[middle_node].nickname,
        cons_rel_stats[middle_node].fingerprint))
    
    return {'time':circ_time,\
            'fast':circ_fast,\
            'stable':circ_stable,\
            'internal':circ_internal,\
            'dirty_time':None,\
            'path':(guard_node, middle_node, exit_node),\
            'cons_rel_stats':cons_rel_stats,\
            'descriptors':descriptors,\
            'covering':[]}
    
    
def create_circuits(consensus_files, processed_descriptor_files, streams,\
    num_samples):
    """Takes streams over time and creates circuits by interaction
    with choose_path().
      Input:
        consensus_files: list of consensus filenames *in correct order*, must
                        exactly cover a time period (i.e. no gaps or overlaps)
        processed_descriptor_files: descriptors corresponding to relays in
            consensus_files as produced by process_consensuses      
        streams: *ordered* list of streams, where a stream is a dict with keys
            'time': timestamp of when stream request occurs 
            'type': with value either
                'resolve' for domain name resolution or
                'generic' for all other TCP connections
            'ip': IP address of destination, may be None for 'type':'resolve'
            'port': desired TCP port, may be None for 'type':'resolve'
        num_samples: (int) # circuit-creation samples to take for given streams
    Output:
        [Prints circuit and guard selections of clients.]
    """
    
    ### Tor parameters ###
    # max age of a dirty circuit to which new streams can be assigned
    dirty_circuit_lifetime = 10*60
    
    # long-lived ports (taken from path-spec.txt)
    long_lived_ports = [21, 22, 706, 1863, 5050, 5190, 5222, 5223, 6667,\
        6697, 8300]
        
    # observed port creates a need active for a limited amount of time
    port_need_lifetime = 60*60 # need expires after an hour

    # time a guard can stay down until it is removed from list    
    guard_down_time = 30*24*3600 # time guard can be down until is removed    
    
    ### Client states for each sample ###
    client_states = []
    for i in range(num_samples):
        # guard is fingerprint -> {'expires':exp_time, 'bad_since':bad_since}
        # port_needs are ports that must be covered by existing circuits        
        # internal_covered_count just ensures a clean internal
        # circuits stored all circuits created
        # live_circuits listed by age, circuit live if it's clean or younger
        #   than dirty_circuit_lifetime
        client_states.append({'id':i,\
                            'guards':{},\
                            'port_needs':{80:{'covered_count':0,\
                                'expires':None, 'fast':True, 'stable':False}},\
                            'circuits':[],\
                            'clean_exit_circuits':collections.deque(),
                            'dirty_exit_circuits':collections.deque(),
                            'clean_internal_circuit':None,
                            'dirty_internal_circuit':None})
    
    ### Simulation variables ###
    cur_period_start = None
    cur_period_end = None
    stream_start = 0
    stream_end = 0
    
    
    # run simulation period one pair of consensus/descriptor files at a time
    for c_file, d_file in zip(consensus_files, processed_descriptor_files):
        # read in descriptors and consensus statuses
        print('Using consensus file {0}'.format(c_file))
        descriptors = {}
        cons_rel_stats = {}
        cons_valid_after = None
        cons_fresh_until = None
        cons_bw_weights = None
        cons_bwweightscale = None
        with open(d_file) as df, open(c_file) as cf:
            for desc in sd.parse_file(df, validate=False):
                descriptors[desc.fingerprint] = desc
            for rel_stat in sd.parse_file(cf, validate=False):
                if (cons_valid_after == None):
                    cons_valid_after = timestamp(rel_stat.document.valid_after)
                    if (cur_period_start == None):
                        cur_period_start = cons_valid_after
                    elif (cur_period_end == cons_valid_after):
                        cur_period_start = cons_valid_after
                    else:
                        err = 'Gap/overlap in consensus times: {0}:{1}'.\
                                format(cur_period_end, cons_valid_after)
                        raise ValueError(err)
                if (cons_fresh_until == None):
                    cons_fresh_until = timestamp(rel_stat.document.fresh_until)
                    cur_period_end = cons_fresh_until
                if (cons_bw_weights == None):
                    cons_bw_weights = rel_stat.document.bandwidth_weights
                if (cons_bwweightscale == None):
                    if ('bwweightscale' in rel_stat.document.params):
                        cons_bwweightscale = rel_stat.document.params[\
                            'bwweightscale']
                if (rel_stat.fingerprint in descriptors):
                    cons_rel_stats[rel_stat.fingerprint] = rel_stat
            if (cons_bwweightscale == None):
                # set default value
                # Yes, I could have set it initially to this value,
                # but this way, it doesn't get repeatedly set.
                cons_bwweightscale = 10000  
                
        for client_state in client_states:
            print('Updating state for client {0}.'.format(client_state['id']))
            guards = client_state['guards']
                
            # update client state
            # Tor does this stuff whenever a descriptor is obtained        
            for guard, guard_props in guards.items():
                # set guard as down if (following Tor's
                # entry_guard_set_status)
                # - not in current nodelist (!node check)
                #   - note that a node can appear the nodelist but not
                #     in consensus if it has an existing descriptor
                #     in routerlist (unclear to me when this gets purged)
                # - Running flag not set
                #   - note that all nodes not in current consensus get
                #     *all* their node flags set to zero
                # - Guard flag not set [and not a bridge])
                # note that hibernating *not* considered here
                if (guard_props['bad_since'] == None):
                    if (guard not in cons_rel_stats) or\
                        (stem.Flag.RUNNING not in\
                         cons_rel_stats[guard].flags) or\
                        (stem.Flag.GUARD not in\
                         cons_rel_stats[guard].flags):
                        print('Putting down guard {0}'.format(guard))
                        guard_props['bad_since'] = cons_valid_after
                else:
                    if (guard in cons_rel_stats) and\
                        (stem.Flag.RUNNING not in\
                         cons_rel_stats[guard].flags) and\
                        (stem.Flag.GUARD not in\
                         cons_rel_stats[guard].flags):
                        print('Bringing up guard {0}'.format(guard))
                        guard_props['bad_since'] = None
                # remove if down time including this period exceeds limit
                if (guard_props['bad_since'] != None):
                    if (cons_fresh_until-guard_props['bad_since'] >=\
                        guard_down_time):
                        print('Guard down too long, removing: {0}'.\
                            format(guard))
                        del guards[guard]
                # expire old guards
                if (guard_props['expires'] <= cons_valid_after):
                    print('Expiring guard: {0}'.format(guard))
                    del guards[guard]
                              
        # apply basic exit filter
        potential_exits = filter_exits(cons_rel_stats, descriptors)
        potential_middles = filter_middles(cons_rel_stats, descriptors)        
        
        # pre-compute weights at different positions
        middle_weights = get_position_weights(cons_rel_stats.keys(),\
            cons_rel_stats, 'm', cons_bw_weights, cons_bwweightscale)
        exit_weights = get_position_weights(cons_rel_stats.keys(),\
            cons_rel_stats, 'e', cons_bw_weights, cons_bwweightscale)            
       
        # for simplicity, step through time one minute at a time
        time_step = 60
        cur_time = cur_period_start
        while (cur_time < cur_period_end):    
            # collect streams that occur during current period
            while (stream_start < len(streams)) and\
                (streams[stream_start]['time'] < cur_time):
                stream_start += 1
            stream_end = stream_start
            while (stream_end < len(streams)) and\
                (streams[stream_end]['time'] < cur_time + time_step):
                stream_end += 1

            for client_state in client_states:
                print('Client {0}.'.format(client_state['id']))
                guards = client_state['guards']
                #circuits = client_state['circuits'] # just printing for now
                dirty_exit_circuits = client_state['dirty_exit_circuits']
                clean_exit_circuits = client_state['clean_exit_circuits']
                dirty_internal_circuit = client_state['dirty_internal_circuit']
                clean_internal_circuit = client_state['clean_internal_circuit']
            
                # kill old dirty circuits
                while (len(dirty_exit_circuits)>0) and\
                        (dirty_exit_circuits[-1]['dirty_time'] <=\
                            cur_time - dirty_circuit_lifetime):
                    print('Killed exit circuit at time {0} w/ dirty time {1}'.\
                            format(cur_time,\
                                dirty_exit_circuits[-1]['dirty_time']))
                    dirty_exit_circuits.pop()
                if (dirty_internal_circuit != None) and\
                    (dirty_internal_circuit['dirty_time'] <=\
                        cur_time - dirty_circuit_lifetime):
                    print('Killed internal circuit at time {0} w/ dirty time\
{1}'.format(cur_time,dirty_internal_circuit['dirty_time']))
                    dirty_internal_circuit = None
                    
                # expire port needs
                port_needs = client_state['port_needs']                
                for port, need in port_needs.items():
                    if (need['expires'] != None) and\
                        (need['expires'] <= cur_time):
                        del port_needs[port]
                
                # cover uncovered ports
                for port, need in port_needs.items():
                    if (need['covered_count'] == 0):
                        # we need to make a new circuit
                        new_circ = create_circuit(cons_rel_stats,\
                            cons_valid_after, cons_fresh_until,\
                            cons_bw_weights, cons_bwweightscale,\
                            descriptors, guards, cur_time, need['fast'],\
                            need['stable'], False, None, port,\
                            potential_exits, potential_middles, exit_weights,\
                            middle_weights)
                        #circuits.append(new_circ)
                        clean_exit_circuits.appendleft(new_circ)
                        # have new_circ cover all port it can
                        for pt, nd in port_needs.items():
                            if (circuit_covers_port_need(new_circ,\
                                descriptors, pt, nd)):
                                nd['covered_count'] += 1
                                new_circ['covering'].append(pt)
                        print('Created circuit at time {0} to cover port \
{1}.'.format(cur_time, port))

                # check for internal circuit
                if (clean_internal_circuit == None):
                    # create new internal circuit
                    client_state['clean_internal_circuit'] =\
                        create_circuit(cons_rel_stats,\
                            cons_valid_after, cons_fresh_until,\
                            cons_bw_weights, cons_bwweightscale, descriptors,\
                            guards, cur_time, True, True, True, None, None,\
                            potential_exits, potential_middles, exit_weights,\
                            middle_weights)
                    clean_internal_circuit =\
                        client_state['clean_internal_circuit']
                    #circuits.append(new_circ)
                    print('Created clean internal circuit at time {0}.'.\
                        format(cur_time))                        
                
                # assign streams in this minute to circuits
                for stream_idx in range(stream_start, stream_end):
                    stream = streams[stream_idx]
                    if (stream['type'] == 'resolve'):
                        if (dirty_internal_circuit != None):
                            # use existing dirty internal circuit
                            print('Assigned stream to dirty internal circuit \
at {0}'.format(stream['time']))
                        elif (clean_internal_circuit != None):
                            # dirty clean internal circuit
                            clean_internal_circuit['dirty_time'] =\
                                stream['time']
                            client_state['dirty_internal_circuit'] =\
                                clean_internal_circuit
                            dirty_internal_circuit = clean_internal_circuit
                            client_state['clean_internal_circuit'] = None
                            clean_internal_circuit =\
                                client_state['clean_internal_circuit']
                            print('Assigned stream to clean internal circuit \
at {0}'.format(stream['time']))
                        else:
                            # create new internal circuit
                            client_state['dirty_internal_circuit'] =\
                                create_circuit(cons_rel_stats,\
                                    cons_valid_after, cons_fresh_until,\
                                    cons_bw_weights, cons_bwweightscale,\
                                    descriptors, guards, stream['time'], True,\
                                    True, True, None, None, potential_exits,\
                                    potential_middles, exit_weights,\
                                    middle_weights)
                            dirty_internal_circuit =\
                                client_state['dirty_internal_circuit']
                            dirty_internal_circuit['dirty_time'] =\
                                stream['time']
                            print('Created new internal circuit for stream \
at {0}'.format(stream['time']))                               
                    elif (stream['type'] == 'generic'):
                        stream_assigned = None                    
                        # try to use a dirty circuit
                        for circuit in dirty_exit_circuits:
                            if circuit_supports_stream(circuit, stream,\
                                long_lived_ports):
                                stream_assigned = circuit
                                print('Assigned stream to port {0} at \
{1}'.format(stream['port'],stream['time']))                                    
                                break        
                        # next try and use a clean circuit
                        if (stream_assigned == None):
                            new_clean_exit_circuits = collections.deque()
                            while (len(clean_exit_circuits) > 0):
                                circuit = clean_exit_circuits.popleft()
                                if (circuit_supports_stream(circuit, stream,\
                                    long_lived_ports)):
                                    stream_assigned = circuit
                                    circuit['dirty_time'] = stream['time']
                                    dirty_exit_circuits.appendleft(circuit)
                                    new_clean_exit_circuits.extend(\
                                        clean_exit_circuits)
                                    clean_exit_circuits.clear()
                                    print('Assigned stream to port {0} at \
{1}'.format(stream['port'], stream['time']))                                
                                        
                                    # reduce cover count for covered port needs
                                    for port in circuit['covering']:
                                        if (port in port_needs):
                                            port_needs[port]['covered_count'] \
                                                -= 1
                                else:
                                    new_clean_exit_circuits.append(circuit)
                            client_state['clean_exit_circuits'] =\
                                new_clean_exit_circuits
                            clean_exit_circuits = new_clean_exit_circuits
                        # if stream still unassigned we must make new circuit
                        if (stream_assigned == None):
                            stable = (stream['port'] in long_lived_ports)
                            new_circ = create_circuit(cons_rel_stats,\
                                cons_valid_after, cons_fresh_until,\
                                cons_bw_weights, cons_bwweightscale,\
                                descriptors, guards, stream['time'], True,\
                                stable, False, stream['ip'], stream['port'],\
                                potential_exits, potential_middles,\
                                exit_weights, middle_weights)
                            new_circ['dirty_time'] = stream['time']
                            stream_assigned = new_circ
                            #circuits.append(new_circ)
                            dirty_exit_circuits.appendleft(new_circ)
                            print('Created circuit at time {0} to cover stream\
 to ip {1} and port {2}.'.format(stream['time'], stream['ip'], stream['port'])) 
 
                         # add need/extend expiration for ports in streams
                        port = stream['port']
                        if (port in port_needs):
                            if (port_needs[port]['expires'] != None) and\
                                (port_needs[port]['expires'] <\
                                    stream['time'] + port_need_lifetime):
                                port_needs[port]['expires'] = stream['time'] +\
                                    port_need_lifetime
                        else:
                            port_needs[port] = {\
                                'covered_count':0,\
                                'expires':(stream['time']+port_need_lifetime),\
                                'fast':True,\
                                'stable':(port in long_lived_ports)}
                    else:
                        raise ValueError('Stream type not recognized: {0}'.\
                            format(stream['type']))
            
            cur_time += time_step

    
        # Specifically, on startup Tor tries to maintain one clean
        # fast exit circuit that allows connections to port 80, and at least
        # two fast clean stable internal circuits in case we get a resolve
        # request...
        # After that, Tor will adapt the circuits that it preemptively builds
        # based on the requests it sees from the user: it tries to have two
        # fast
        # clean exit circuits available for every port seen within the past
        # hour
        # (each circuit can be adequate for many predicted ports -- it doesn't
        # need two separate circuits for each port), and it tries to have the
        # above internal circuits available if we've seen resolves or hidden
        # service activity within the past hour...
        # Additionally, when a client request exists that no circuit (built or
        # pending) might support, we create a new circuit to support the
        # request.
        # For exit connections, we pick an exit node that will handle the
        # most pending requests (choosing arbitrarily among ties) 
    
    
if __name__ == '__main__':
    command = None
    usage = 'Usage: pathsim.py [command]\nCommands:\n\tprocess: Pair consensuses with recent descriptors.\n\tsimulate [in] [out] [# samples]: Do a bunch of simulated path selections using consensus from [in], processed descriptors from [out], and taking [# samples] samples.'
    if (len(sys.argv) <= 1):
        print(usage)
        sys.exit(1)
    else:
        command = sys.argv[1]
        if (command != 'process') and (command != 'simulate'):
            print(usage)

    if (command == 'process'):
        descriptor_dir = ['in/descriptors']
        consensus_dir = 'in/consensuses'
        out_dir = 'out/descriptors'
        process_consensuses(descriptor_dir, consensus_dir, out_dir)    
    elif (command == 'simulate'):
        # get lists of consensuses and the related processed-descriptor files 
        if (len(sys.argv) >= 3):
            consensus_dir = sys.argv[2]
        else:
            consensus_dir = 'in/consensuses'
        if (len(sys.argv) >= 4):
            descriptor_dir = sys.argv[3]
        else:
            descriptor_dir = 'out/processed-descriptors'
        if (len(sys.argv) >= 5):
            num_samples = int(sys.argv[4])
        else:
            num_samples = 1
        
        consensus_files = []
        for dirpath, dirnames, filenames in os.walk(consensus_dir):
            for filename in filenames:
                if (filename[0] != '.'):
                    consensus_files.append(os.path.join(dirpath,filename))
        consensus_files.sort()
        
        processed_descriptor_files = []
        for dirpath, dirnames, filenames in os.walk(descriptor_dir):
            for filename in filenames:
                if (filename[0] != '.'):
                    processed_descriptor_files.append(\
                        os.path.join(dirpath,filename))
        processed_descriptor_files.sort()

        # determine start and end times
        start_time = None
        with open(consensus_files[0]) as cf:
            for rel_stat in sd.parse_file(cf, validate=False):
                if (start_time == None):
                    start_time =\
                        timestamp(rel_stat.document.valid_after)
                    break
        end_time = None
        with open(consensus_files[-1]) as cf:
            for rel_stat in sd.parse_file(cf, validate=False):
                if (end_time == None):
                    end_time =\
                        timestamp(rel_stat.document.fresh_until)
                    break        

        # simple user that makes a port 80 request & resolve every x seconds
        http_request_rate = 5 * 60
        str_ip = '74.125.131.105' # www.google.com
        t = start_time
        streams = []
        while (t < end_time):
            streams.append({'time':t,'type':'resolve','ip':None,'port':None})
            streams.append({'time':t,'type':'generic','ip':str_ip,'port':80})
            t += http_request_rate
        create_circuits(consensus_files, processed_descriptor_files, streams,\
            num_samples)    

# TODO
# - support IPv6 addresses
# - add DNS requests
# - We do not consider removing stable/fast requirements if a suitable relay can't be found at some point. Tor does this. Rather, we just error.
# - Instead of immediately using a new consensus, set a random time to
#   switch to the new one, following the process in dir-spec.txt (Sec. 5.1).
# - Check for descriptors that aren't the ones in the consensus, particularly
#   those older than 48 hours, which should expire (dir-spec.txt, Sec. 5.2).
# - Implement that circuits made on demand use Fast.
# - Implement (or indicate) fail-back if enough fast/stable nodes not found.
# - Implement max limit (12 in path-spec.txt) on # circuits.
# - Figure out if and when clean circuits are torn down.
# - Implement user non-activity for an hour means _no_ preemptive circuits.


##### Relevant lines for path selection extracted from Tor specs.

# Circuit creation according to path-spec.txt
# Specifically, on startup Tor tries to maintain one clean
# fast exit circuit that allows connections to port 80, and at least
# two fast clean stable internal circuits in case we get a resolve
# request...
# After that, Tor will adapt the circuits that it preemptively builds
# based on the requests it sees from the user: it tries to have two
# fast
# clean exit circuits available for every port seen within the past
# hour
# (each circuit can be adequate for many predicted ports -- it doesn't
# need two separate circuits for each port), and it tries to have the
# above internal circuits available if we've seen resolves or hidden
# service activity within the past hour...
# Additionally, when a client request exists that no circuit (built or
# pending) might support, we create a new circuit to support the
# request.
# For exit connections, we pick an exit node that will handle the
# most pending requests (choosing arbitrarily among ties) 

# Path selection according to dir-spec.txt
# 1. Clients SHOULD NOT use non-'Valid' or non-'Running' routers
# 2. Clients SHOULD NOT use non-'Fast' routers for any purpose other than
#    very-low-bandwidth circuits (such as introduction circuits).
# 3. Clients SHOULD NOT use non-'Stable' routers for circuits that are
#    likely to need to be open for a very long time
# 4. Clients SHOULD NOT choose non-'Guard' nodes when picking entry guard
# 5. if the [Hibernate] value is 1, then the Tor relay was hibernating when
#    the descriptor was published, and shouldn't be used to build circuits."    

# Path selection according to path-spec.txt
# 1. We weight node selection according to router bandwidth
# 2. We also weight the bandwidth of Exit and Guard flagged
# nodes       
# depending on the fraction of total bandwidth that they make
#up 
# and depending upon the position they are being selected for.
# 4. IP address and port. If dest. IP is unknown, we need to
# pick    
# an exit node that "might support" connections to a
# given address port with an unknown address.  An exit node
# "might 
# support" such a connection if any clause that accepts any 
# connections to that port precedes all clauses that reject all       
# connections to that port.
# 5. We never choose an exit node flagged as "BadExit"
# ...
# 6. We do not choose the same router twice for the same path.
# 7. We do not choose any router in the same family as another in the same
#    path.
# 8. We do not choose more than one router in a given /16 subnet
#    (unless EnforceDistinctSubnets is 0).
# 9. We don't choose any non-running or non-valid router unless we have
#    been configured to do so. By default, we are configured to allow
#    non-valid routers in "middle" and "rendezvous" positions.
# 10. If we're using Guard nodes, the first node must be a Guard (see 5
#     below)
# ...
# [Sec. 5]
#  A guard is unusable if any of the following hold:
#    - it is not marked as a Guard by the networkstatuses,
#    - it is not marked Valid (and the user hasn't set AllowInvalid
#    - it is not marked Running
#    - Tor couldn't reach it the last time it tried to connect
