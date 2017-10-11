#!/usr/bin/env python3
"""
Executes state tests on multiple clients, checking for EVM trace equivalence

"""
import json, sys, re, os, subprocess, io, itertools
from contextlib import redirect_stderr, redirect_stdout
import ethereum.transactions as transactions
from ethereum.utils import decode_hex, parse_int_or_hex, sha3, to_string, \
    remove_0x_head, encode_hex, big_endian_to_int

from evmlab import genesis as gen
from evmlab import vm as VMUtils
from evmlab import opcodes

import logging
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)

cfg ={}

def parse_config():
    """Parses 'statetests.ini'-file, which 
    may contain user-specific configuration
    """

    import configparser
    config = configparser.ConfigParser()
    config.read('statetests.ini')
    import getpass
    uname = getpass.getuser()
    if uname not in config.sections():
        uname = "DEFAULT"

    cfg['RANDOM_TESTS'] = config[uname]['random_tests']
    cfg['DO_CLIENTS']  = config[uname]['clients'].split(",")
    cfg['FORK_CONFIG'] = config[uname]['fork_config']
    cfg['TESTS_PATH']  = config[uname]['tests_path']
    cfg['PYETH_DOCKER_NAME'] = config[uname]['pyeth_docker_name']
    cfg['CPP_DOCKER_NAME'] = config[uname]['cpp_docker_name']
    cfg['PARITY_DOCKER_NAME'] = config[uname]['parity_docker_name']
    cfg['GETH_DOCKER_NAME'] = config[uname]['geth_docker_name']
    cfg['PRESTATE_TMP_FILE']=config[uname]['prestate_tmp_file']
    cfg['SINGLE_TEST_TMP_FILE']=config[uname]['single_test_tmp_file']
    cfg['LOGS_PATH'] = config[uname]['logs_path']
    cfg['TESTETH_DOCKER_NAME'] = config[uname]['testeth_docker_name']
    logger.info("Config")
    logger.info("\tActive clients: %s",      cfg['DO_CLIENTS'])
    logger.info("\tFork config: %s",         cfg['FORK_CONFIG'])
    logger.info("\tTests path: %s",          cfg['TESTS_PATH'])
    logger.info("\tPyeth: %s",               cfg['PYETH_DOCKER_NAME'])
    logger.info("\tCpp: %s",                 cfg['CPP_DOCKER_NAME'])
    logger.info("\tParity: %s",              cfg['PARITY_DOCKER_NAME'])
    logger.info("\tGeth: %s",                cfg['GETH_DOCKER_NAME'])
    logger.info("\tPrestate tempfile: %s",   cfg['PRESTATE_TMP_FILE'])
    logger.info("\tSingle test tempfile: %s",cfg['SINGLE_TEST_TMP_FILE'])
    logger.info("\tLog path: %s",            cfg['LOGS_PATH'])



parse_config()


# used to check for unknown opcode names in traces
OPCODES = {}
op_keys = opcodes.opcodes.keys()
for op_key in op_keys:
    if op_key in opcodes.opcodesMetropolis and cfg['FORK_CONFIG'] != 'Byzantium':
        continue
    name = opcodes.opcodes[op_key][0]
    # allow opcode lookups by either name or number assignment
    OPCODES[name] = op_key
    OPCODES[op_key] = name



def iterate_tests(path = '/GeneralStateTests/', ignore = []):
    logging.info (cfg['TESTS_PATH'] + path)
    for subdir, dirs, files in sorted(os.walk(cfg['TESTS_PATH'] + path)):
        for f in files:
            if f.endswith('json'):
                for ignore_name in ignore:
                    if f.find(ignore_name) != -1:
                        continue
                    yield os.path.join(subdir, f)


def convertGeneralTest(test_file, fork_name):
    # same default as evmlab/genesis.py
    metroBlock = 2000
    if fork_name == 'Byzantium':
        metroBlock = 0


    with open(test_file) as json_data:
        general_test = json.load(json_data)
        for test_name in general_test:
            # should only be one test_name per file
            prestate = {
                'env' : general_test[test_name]['env'],
                'pre' : general_test[test_name]['pre'],
                'config' : { # for pyeth run_statetest.py
                    'metropolisBlock' : 2000, # same default as evmlab/genesis.py
                    'eip158Block' : 2000,
                    'eip150Block' : 2000,
                    'eip155Block' : 2000,
                    'homesteadBlock' : 2000,
                }
            }
            if cfg['FORK_CONFIG'] == 'Byzantium':
                prestate['config'] = {
                    'metropolisBlock' : 0,
                    'eip158Block' : 0,
                    'eip150Block' : 0,
                    'eip155Block' : 0,
                    'homesteadBlock' : 0,
                }
            if cfg['FORK_CONFIG'] == 'Homestead':
                prestate['config']['homesteadBlock'] = 0
            #print("prestate:", prestate)
            general_tx = general_test[test_name]['transaction']
            transactions = []
            for test_i in general_test[test_name]['post'][fork_name]:
                test_tx = general_tx.copy()
                d_i = test_i['indexes']['data']
                g_i = test_i['indexes']['gas']
                v_i = test_i['indexes']['value']
                test_tx['data'] = general_tx['data'][d_i]
                test_tx['gasLimit'] = general_tx['gasLimit'][g_i]
                test_tx['value'] = general_tx['value'][v_i]
                test_dgv = (d_i, g_i, v_i)
                transactions.append((test_tx, test_dgv))

        return prestate, transactions


def selectSingleFromGeneral(single_i, general_testfile, fork_name):
    # a fork/network in a general state test has an array of test cases
    # each element of the array specifies (d,g,v) indexes in the transaction
    with open(general_testfile) as json_data:
        general_test = json.load(json_data)
        #logger.info("general_test: %s", general_test)
        for test_name in general_test:
            # should only be one test_name per file
            single_test = general_test
            single_tx = single_test[test_name]['transaction']
            general_tx = single_test[test_name]['transaction']
            selected_case = general_test[test_name]['post'][fork_name][single_i]
            single_tx['data'] = [ general_tx['data'][selected_case['indexes']['data']] ]
            single_tx['gasLimit'] = [ general_tx['gasLimit'][selected_case['indexes']['gas']] ]
            single_tx['value'] = [ general_tx['value'][selected_case['indexes']['value']] ]
            selected_case['indexes']['data'] = 0
            selected_case['indexes']['gas'] = 0
            selected_case['indexes']['value'] = 0
            single_test[test_name]['post'] = {}
            single_test[test_name]['post'][fork_name] = []
            single_test[test_name]['post'][fork_name].append(selected_case)
            return single_test



def getIntrinsicGas(test_tx):
    tx = transactions.Transaction(
        nonce=parse_int_or_hex(test_tx['nonce'] or b"0"),
        gasprice=parse_int_or_hex(test_tx['gasPrice'] or b"0"),
        startgas=parse_int_or_hex(test_tx['gasLimit'] or b"0"),
        to=decode_hex(remove_0x_head(test_tx['to'])),
        value=parse_int_or_hex(test_tx['value'] or b"0"),
        data=decode_hex(remove_0x_head(test_tx['data'])))

    return tx.intrinsic_gas_used

def getTxSender(test_tx):
    tx = transactions.Transaction(
        nonce=parse_int_or_hex(test_tx['nonce'] or b"0"),
        gasprice=parse_int_or_hex(test_tx['gasPrice'] or b"0"),
        startgas=parse_int_or_hex(test_tx['gasLimit'] or b"0"),
        to=decode_hex(remove_0x_head(test_tx['to'])),
        value=parse_int_or_hex(test_tx['value'] or b"0"),
        data=decode_hex(remove_0x_head(test_tx['data'])))
    if 'secretKey' in test_tx:
        tx.sign(decode_hex(remove_0x_head(test_tx['secretKey'])))
    return encode_hex(tx.sender)

def canon(str):
    if str in [None, "0x", ""]:
        return ""
    if str[:2] == "0x":
        return str
    return "0x" + str

def toText(op):
    return VMUtils.toText(op)

def dumpJson(obj, dir = None, prefix = None):
    import tempfile
    fd, temp_path = tempfile.mkstemp(prefix = 'randomtest_', suffix=".json", dir = dir)
    with open(temp_path, 'w') as f :
        json.dump(obj,f)
        logger.info("Saved file to %s" % temp_path)
    os.close(fd)
    return temp_path

def createRandomStateTest():
    cmd = ["docker", "run", "--rm", cfg['TESTETH_DOCKER_NAME'],"-t","GeneralStateTests","--","--createRandomTest"]
    outp = "".join(VMUtils.finishProc(VMUtils.startProc(cmd)))
    #Validate that it's json
    return json.loads(outp)


def generateTests():
    import getpass, time
    uname = getpass.getuser()
    host_id = "%s-%s" % (uname, time.strftime("%a_%H_%M_%S"))
    here = os.path.dirname(os.path.realpath(__file__))

    cfg['TESTS_PATH'] = "%s/generatedTests/" % here
    testfile_dir = "%s/generatedTests/GeneralStateTests/stRandom" % here
    filler_dir = "%s/generatedTests/src/GeneralStateTestsFiller/stRandom" % here 
    os.makedirs( testfile_dir , exist_ok = True)
    os.makedirs( filler_dir, exist_ok = True)
    import pathlib

    counter = 0
    while True: 
        identifier = "%s-%d" %(host_id, counter)
        test_json =  createRandomStateTest()
        test_fullpath = "%s/randomStatetest%s.json" % (testfile_dir, identifier)
        filler_fullpath = "%s/randomStatetest%sFiller.json" % (filler_dir, identifier)
        test_json['randomStatetest%s' % identifier] =test_json.pop('randomStatetest', None) 

        
        with open(test_fullpath, "w+") as f:
            json.dump(test_json, f)
            pathlib.Path(filler_fullpath).touch()

        yield test_fullpath
        counter = counter +1

def startParity(test_file):

    testfile_path = os.path.abspath(test_file)
    mount_testfile = testfile_path + ":" + "/mounted_testfile"

    cmd = ["docker", "run", "--rm", "-t", "-v", mount_testfile, cfg['PARITY_DOCKER_NAME'], "--json", "--statetest", "/mounted_testfile"]
   
    return {'proc':VMUtils.startProc(cmd), 'cmd': " ".join(cmd)}


def startCpp(test_subfolder, test_name, test_dgv):

    [d,g,v] = test_dgv

    cpp_mount_tests = cfg['TESTS_PATH'] + ":" + "/mounted_tests"

    cmd = ["docker", "run", "--rm", "-t", "-v", cpp_mount_tests, cfg['CPP_DOCKER_NAME']
            ,'-t',"GeneralStateTests/%s" %  test_subfolder
            ,'--'
            ,'--singletest', test_name
            ,'--jsontrace',"'{ \"disableStorage\":true, \"disableMemory\":true }'"
            ,'--singlenet',cfg['FORK_CONFIG']
            ,'-d',str(d),'-g',str(g), '-v', str(v)
            ,'--testpath', '"/mounted_tests"']

    if cfg['FORK_CONFIG'] == 'Homestead' or cfg['FORK_CONFIG'] == 'Frontier':
        cmd.extend(['--all']) # cpp requires this for some reason

    return {'proc':VMUtils.startProc(cmd), 'cmd': " ".join(cmd)}

def startGeth(test_file):

    testfile_path = os.path.abspath(test_file)
    mount_testfile = testfile_path + ":" + "/mounted_testfile"

    cmd = ["docker", "run", "--rm", "-t", "-v", mount_testfile, cfg['GETH_DOCKER_NAME'], "--json", "--nomemory", "statetest", "/mounted_testfile"]

    return {'proc':VMUtils.startProc(cmd), 'cmd': " ".join(cmd)}



def startPython(test_file, test_tx):

    tx_encoded = json.dumps(test_tx)
    tx_double_encoded = json.dumps(tx_encoded) # double encode to escape chars for command line

    # command if not using a docker container
    # pyeth_process = subprocess.Popen(["python", "run_statetest.py", test_file, tx_double_encoded], shell=False, stdout=subprocess.PIPE, close_fds=True)

    # command to run docker container
    # docker run --volume=/absolute/path/prestate.json:/mounted_prestate cdetrio/pyethereum run_statetest.py mounted_prestate "{\"data\": \"\", \"gasLimit\": \"0x0a00000000\", \"gasPrice\": \"0x01\", \"nonce\": \"0x00\", \"secretKey\": \"0x45a915e4d060149eb4365960e6a7a45f334393093061116b197e3240065ff2d8\", \"to\": \"0x0f572e5295c57f15886f9b263e2f6d2d6c7b5ec6\", \"value\": \"0x00\"}"

    prestate_path = os.path.abspath(test_file)
    mount_flag = prestate_path + ":" + "/mounted_prestate"
    cmd = ["docker", "run", "--rm", "-t", "-v", mount_flag, cfg['PYETH_DOCKER_NAME'], "run_statetest.py", "/mounted_prestate", tx_double_encoded]

    return {'proc':VMUtils.startProc(cmd), 'cmd': " ".join(cmd)}



TEST_WHITELIST = []


SKIP_LIST = [
    #'modexp_*', # regex example
    'POP_Bounds',
    'POP_BoundsOOG',
    'MLOAD_Bounds',
    'Call1024PreCalls', # Call1024PreCalls does produce a trace difference, worth fixing that trace
    'createInitFailStackSizeLargerThan1024',
    'createJS_ExampleContract',
    'CALL_Bounds',
    'mload32bitBound_Msize ',
    'mload32bitBound_return2',
    'Call1MB1024Calldepth ',
    'shallowStackOK',
    'stackOverflowM1PUSH', # slow
    'static_Call1MB1024Calldepth', # slow
    'static_Call1024BalanceTooLow',
    'static_Call1024BalanceTooLow2',
    'static_Call1024OOG',
    'static_Call1024PreCalls',
    'static_Call1024PreCalls2', # slow
    'static_Call1024PreCalls3', #slow
    'static_Call50000',
    'static_Call50000bytesContract50_1',
    'static_Call50000bytesContract50_2',
    'static_Call50000bytesContract50_3',
    'static_CallToNameRegistratorAddressTooBigLeft',
    'static_Call50000_identity2',
    'static_Call50000_identity',
    'static_Call50000_ecrec',
    'static_Call50000_rip160',
    'static_Call50000_sha256',
    'static_Return50000_2',
    'static_callChangeRevert',
    'static_log3_MaxTopic',
    'static_log4_Caller',
    'static_RawCallGas',
    'static_RawCallGasValueTransfer',
    'static_RawCallGasValueTransferAsk',
    'static_RawCallGasValueTransferMemory',
    'static_RawCallGasValueTransferMemoryAsk',
    'static_refund_CallA_notEnoughGasInCall',
    'static_LoopCallsThenRevert',
    'HighGasLimit', # geth doesn't run
    'zeroSigTransacrionCreate', # geth fails this one
    'zeroSigTransacrionCreatePrice0', # geth fails
    'zeroSigTransaction', # geth fails
    'zeroSigTransaction0Price', # geth fails
    'zeroSigTransactionInvChainID',
    'zeroSigTransactionInvNonce',
    'zeroSigTransactionInvNonce2',
    'zeroSigTransactionOOG',
    'zeroSigTransactionOrigin',
    'zeroSigTransactionToZero',
    'zeroSigTransactionToZero2',
    'OverflowGasRequire2',
    'TransactionDataCosts652',
    'stackLimitPush31_1023',
    'stackLimitPush31_1023',
    'stackLimitPush31_1024',
    'stackLimitPush31_1025', # test runner crashes
    'stackLimitPush32_1023',
    'stackLimitPush32_1024',
    'stackLimitPush32_1025', # big trace, onsensus failure
    'stackLimitGas_1023',
    'stackLimitGas_1024', # consensus bug
    'stackLimitGas_1025'
]

regex_skip = [skip.replace('*', '') for skip in SKIP_LIST if '*' in skip]



# to resume running after interruptions
START_I = 0


def testIterator():
    if cfg['RANDOM_TESTS'] == 'Yes':
        logger.info("generating random tests...")
        return generateTests()
    else:
        logger.info("iterating over state tests...")
        return iterate_tests(ignore=['stMemoryTest','stMemoryTest','stMemoryTest'])


def main():
    fail_count = 0
    pass_count = 0
    failing_files = []
    test_number = 0

    for f in testIterator():
        with open(f) as json_data:
            general_test = json.load(json_data)
            test_name = list(general_test.keys())[0]
            if TEST_WHITELIST and test_name not in TEST_WHITELIST:
                continue
            if test_name in SKIP_LIST and test_name not in TEST_WHITELIST:
                logger.info("skipping test: %s" % test_name)
                continue
            if regex_skip and re.search('|'.join(regex_skip), test_name) and test_name not in TEST_WHITELIST:
                logger.info("skipping test (regex match): %s" % test_name)
                continue


        (test_number, num_fails, num_passes,failures) = perform_test(f, test_name, test_number)

        logger.info("f/p/t: %d,%d,%d" % ( num_fails, num_passes, (num_fails + num_passes)))
        logger.info("failures: %s" % str(failing_files))
        logger.info("tot fail_count: %d" % fail_count)
        logger.info("tot pass_count: %d" % pass_count)
        logger.info("tot           : %d" % (fail_count + pass_count))

        fail_count = fail_count + num_fails
        pass_count = pass_count + num_passes
        failing_files.extend(failures)
        #if fail_count > 0:
        #    break
    # done with all tests. print totals
    logger.info("fail_count: %d" % fail_count)
    logger.info("pass_count: %d" % pass_count)
    logger.info("total:      %d" % (fail_count + pass_count))


def finishProc(name, processInfo, canonicalizer, fulltrace_filename = None):
    """ Ends the process, returns the canonical trace and also writes the 
    full process output to a file, along with the command used to start the process"""

    process = processInfo['proc']

    extraTime = False
    if name == "PY":
        extraTime = True

    outp = VMUtils.finishProc(processInfo['proc'], extraTime)

    if fulltrace_filename is not None:
        #logging.info("Writing %s full trace to %s" % (name, fulltrace_filename))
        with open(fulltrace_filename, "w+") as f: 
            f.write("# command\n")
            f.write("# %s\n\n" % processInfo['cmd'])
            f.write("\n".join(outp))

    canon_text = [toText(step) for step in canonicalizer(outp)]
    logging.info("Processed %s steps for %s" % (len(canon_text), name))
    return canon_text


def perform_test(testfile, test_name, test_number = 0):

    logger.info("file: %s, test name %s " % (testfile,test_name))

    pass_count = 0
    failures = []
    fork_name        = cfg['FORK_CONFIG']
    clients          = cfg['DO_CLIENTS']
    test_tmpfile     = cfg['SINGLE_TEST_TMP_FILE']
    prestate_tmpfile = cfg['PRESTATE_TMP_FILE']

    try:
        prestate, txs_dgv = convertGeneralTest(testfile, fork_name)
    except Exception as e:
        logger.warn("problem with test file, skipping.")
        return (test_number, fail_count, pass_count, failures)

#    logger.info("prestate: %s", prestate)
    logger.debug("txs: %s", txs_dgv)

    with open(prestate_tmpfile, 'w') as outfile:
        json.dump(prestate, outfile)

    test_subfolder = testfile.split(os.sep)[-2]

    for tx_i, tx_and_dgv in enumerate(txs_dgv):
        test_number += 1
        if test_number < START_I and not TEST_WHITELIST:
            continue

        test_id = "{:0>4}-{}-{}-{}".format(test_number,test_subfolder,test_name,tx_i)
        logger.info("test id: %s" % test_id)

        single_statetest = selectSingleFromGeneral(tx_i, testfile, fork_name)
        with open(test_tmpfile, 'w') as outfile:
            json.dump(single_statetest, outfile)

        tx = tx_and_dgv[0]
        tx_dgv = tx_and_dgv[1]


        clients_canon_traces = []
        procs = []

        canonicalizers = {
            "GETH" : VMUtils.GethVM.canonicalized, 
            "CPP"  : VMUtils.CppVM.canonicalized, 
            "PY"   : VMUtils.PyVM.canonicalized, 
            "PAR"  :  VMUtils.ParityVM.canonicalized ,
        }
        logger.info("Starting processes for %s" % clients)

        #Start the processes
        for client_name in clients:

            if client_name == 'GETH':
                procinfo = startGeth(test_tmpfile)
            elif client_name == 'CPP':
                procinfo = startCpp(test_subfolder, test_name, tx_dgv)
            elif client_name == 'PY':
                procinfo = startPython(prestate_tmpfile, tx)
            elif client_name == 'PAR':
                procinfo = startParity(test_tmpfile)
            else:
                logger.warning("Undefined client %s", client_name)
                continue
            procs.append( (procinfo, client_name ))

        traceFiles = []
        # Read the outputs
        for (procinfo, client_name) in procs:
            if procinfo['proc'] is None:
                continue

            canonicalizer = canonicalizers[client_name]
            full_trace_filename = os.path.abspath("%s/%s-%s.trace.log" % (cfg['LOGS_PATH'],test_id, client_name))
            traceFiles.append(full_trace_filename)
            canon_trace = finishProc(client_name, procinfo, canonicalizer, full_trace_filename)
            clients_canon_traces.append(canon_trace)

        (equivalent, trace_output) = VMUtils.compare_traces(clients_canon_traces, clients) 

        if equivalent:
            #delete non-failed traces
            for f in traceFiles:
                os.remove(f)

            pass_count += 1
            passfail = 'PASS'
        else:
            # save the state-test
            statetest_filename = "%s/%s-test.json" %(
                cfg['LOGS_PATH'], 
                test_id)
            os.rename(test_tmpfile,statetest_filename)
            logger.warning("CONSENSUS BUG!!!\a")
            passfail = 'FAIL'
            failures.append(test_name)

        passfail_log_filename = "%s/%s-%s.log.txt" % ( 
            cfg['LOGS_PATH'], 
            passfail,
            test_id)
        with open(passfail_log_filename, "w+") as f:
            logger.info("Combined trace: %s" , passfail_log_filename)
            f.write("\n".join(trace_output))

    return (test_number, len(failures), pass_count, failures)

"""
## need to get redirect_stdout working for the python-afl fuzzer

# currently doPython() spawns a new process, and gets the pyethereum VM trace from the subprocess.Popen shell output.
# python-afl cannot instrument a separate process, so this prevents it from measuring the code/path coverage of pyeth

# TODO: invoke pyeth state test runner as a module (so python-afl can measure path coverage), and use redirect_stdout to get the trace


def runStateTest(test_case):
    _state = init_state(test_case['env'], test_case['pre'])
    f = io.StringIO()
    with redirect_stdout(f):
        computed = compute_state_test_unit(_state, test_case["transaction"], config_spurious)
    f.seek(0)
    py_out = f.read()
    print("py_out:", py_out)
"""


if __name__ == '__main__':
    main()
