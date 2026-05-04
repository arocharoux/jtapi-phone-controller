package acme.jtapi;

// ── Cisco JTAPI extensions ────────────────────────────────────────────────────
import com.cisco.jtapi.extensions.CiscoAddress;
import com.cisco.jtapi.extensions.CiscoTerminal;

// ── Standard JTAPI core interfaces ───────────────────────────────────────────
import javax.telephony.Address;
import javax.telephony.AddressObserver;
import javax.telephony.Call;
import javax.telephony.Connection;
import javax.telephony.JtapiPeer;
import javax.telephony.JtapiPeerFactory;
import javax.telephony.Provider;
import javax.telephony.ProviderObserver;
import javax.telephony.Terminal;
import javax.telephony.TerminalConnection;
import javax.telephony.TerminalObserver;

// ── JTAPI Call Control extensions (supplementary services) ───────────────────
import javax.telephony.callcontrol.CallControlCall;
import javax.telephony.callcontrol.CallControlCallObserver;
import javax.telephony.callcontrol.CallControlConnection;
import javax.telephony.callcontrol.CallControlTerminalConnection;
import javax.telephony.callcontrol.events.CallCtlConnAlertingEv;
import javax.telephony.callcontrol.events.CallCtlTermConnHeldEv;
import javax.telephony.callcontrol.events.CallCtlTermConnRingingEv;
import javax.telephony.callcontrol.events.CallCtlTermConnTalkingEv;

// ── JTAPI event types ────────────────────────────────────────────────────────
import javax.telephony.events.AddrEv;
import javax.telephony.events.CallEv;
import javax.telephony.events.ConnDisconnectedEv;
import javax.telephony.events.ProvEv;
import javax.telephony.events.ProvInServiceEv;
import javax.telephony.events.TermConnActiveEv;
import javax.telephony.events.TermConnRingingEv;
import javax.telephony.events.TermEv;

// ── Media (DTMF generation) ──────────────────────────────────────────────────
import javax.telephony.media.MediaTerminalConnection;

// ── Java standard library ────────────────────────────────────────────────────
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * General-purpose JTAPI phone controller for automated test execution.
 *
 * <h2>Architecture</h2>
 * This controller uses an <b>asynchronous event-driven model</b> to interact with
 * Cisco CUCM via JTAPI. It implements four observer interfaces:
 * <ul>
 *   <li>{@link ProviderObserver}  — monitors JTAPI provider lifecycle (IN_SERVICE / OUT_OF_SERVICE)</li>
 *   <li>{@link TerminalObserver}  — monitors phone device events</li>
 *   <li>{@link AddressObserver}   — monitors directory number (address) registration state</li>
 *   <li>{@link CallControlCallObserver} — receives real-time call state transitions
 *       (TALKING, HELD, RINGING, DISCONNECTED, ALERTING)</li>
 * </ul>
 *
 * Call state events are delivered asynchronously by the JTAPI provider thread and
 * deposited into a shared {@link BlockingQueue}. Command methods block on this queue
 * waiting for the expected event, which decouples command execution from the
 * unpredictable timing of CUCM event delivery.
 *
 * <h2>Operating Modes</h2>
 * <ol>
 *   <li><b>Command-driven mode</b> — reads a sequence of commands from stdin
 *       (one per line, {@code COMMAND key=value ...}). This is the primary mode
 *       used by the Python test automation framework.</li>
 *   <li><b>Legacy mode</b> — when stdin is empty, dispatches based on a positional
 *       {@code operation} argument ({@code inspect_terminal}, {@code outbound_call_flow}).
 *       Retained for backward compatibility with earlier test scripts.</li>
 * </ol>
 *
 * <h2>Command Protocol (stdin)</h2>
 * <pre>
 *   DIAL destination=&lt;number&gt; [timeout=&lt;seconds&gt;]
 *   ANSWER [timeout=&lt;seconds&gt;]
 *   HOLD [timeout=&lt;seconds&gt;]
 *   RESUME [timeout=&lt;seconds&gt;]
 *   TRANSFER destination=&lt;number&gt; [type=consult|blind] [timeout=&lt;seconds&gt;]
 *   CONFERENCE destination=&lt;number&gt; [timeout=&lt;seconds&gt;]
 *   SEND_DTMF digits=&lt;digits&gt;
 *   DISCONNECT
 *   SLEEP seconds=&lt;n&gt;
 *   WAIT state=&lt;TALKING|HELD|RINGING|DISCONNECTED|ALERTING&gt; [timeout=&lt;seconds&gt;]
 *   INSPECT [address_timeout=&lt;seconds&gt;]
 * </pre>
 *
 * <h2>Output Contract</h2>
 * All output is JSON on stdout. Every execution produces exactly one JSON object
 * containing {@code status} ("completed" or "failed"), {@code actions} (commands
 * executed), {@code states} (phone state transitions), and {@code events}
 * (raw JTAPI events observed). Debug/diagnostic messages go to stderr.
 *
 * <h2>Usage</h2>
 * <pre>
 *   java acme.jtapi.PhoneController &lt;provider&gt; &lt;user&gt; &lt;pass&gt; &lt;device&gt; &lt;dn&gt; &lt;operation&gt;
 * </pre>
 *
 * @see phone.py   — Standalone command-line scenario runner
 * @see _runner.py — Python-side compilation and execution wrapper
 */
public final class PhoneController implements ProviderObserver, TerminalObserver,
        AddressObserver, CallControlCallObserver {

    // ── Recording ────────────────────────────────────────────────────────
    // These lists accumulate JSON fragments that are emitted in the final
    // output object on stdout.  Each entry is a self-contained JSON object
    // string with a timestamp, making the output a chronological audit trail.

    /** User-initiated actions performed during this session (dial, hold, etc.). */
    private final List<String> actionEntries = new ArrayList<>();

    /** Phone state transitions observed (IDLE → CONNECTED → HELD → …). */
    private final List<String> stateEntries = new ArrayList<>();

    /** Raw JTAPI events received from the provider and observer callbacks. */
    private final List<String> eventEntries = new ArrayList<>();

    // ── Provider / Terminal / Address ─────────────────────────────────────
    // These are the core JTAPI objects resolved during initialization.
    // Together they represent: which cluster (provider), which physical
    // phone (terminal), and which directory number (address) we control.

    /** JTAPI provider representing the CUCM CTI connection. */
    private Provider provider;

    /** JTAPI terminal representing the target physical phone device (e.g., SEP MAC). */
    private Terminal terminal;

    /** JTAPI address representing the directory number on the target phone. */
    private Address address;

    /** Latch that blocks until the provider reaches IN_SERVICE state (max 60s). */
    private final CountDownLatch providerInService = new CountDownLatch(1);

    /** Latch that blocks until the address reaches IN_SERVICE (used by INSPECT). */
    private final CountDownLatch addressInService = new CountDownLatch(1);

    // ── Live call state ──────────────────────────────────────────────────
    // These track the currently active call and terminal connection across
    // command boundaries.  Updated by observer callbacks on the JTAPI
    // provider thread, read by command methods on the main thread.
    // AtomicReference ensures thread-safe handoff without explicit locking.

    /** The active terminal connection for the controlled phone's leg of the call. */
    private final AtomicReference<CallControlTerminalConnection> activeTermConn = new AtomicReference<>();

    /** The active call object (may be outbound-initiated or inbound-received). */
    private final AtomicReference<Call> activeCall = new AtomicReference<>();

    /**
     * Asynchronous state event queue.  Observer callbacks deposit event names
     * (TALKING, HELD, RINGING, DISCONNECTED, ALERTING) into this queue.
     * Command methods poll it with a timeout to wait for expected transitions.
     * This decouples the JTAPI event delivery thread from command execution.
     */
    private final BlockingQueue<String> stateEvents = new LinkedBlockingQueue<>();

    // ── Entry point ──────────────────────────────────────────────────────

    /**
     * JVM entry point.  Instantiates the controller and delegates to
     * {@link #run(String[])} for initialization and command execution.
     * Calls {@code System.exit(0)} explicitly to ensure the JVM terminates
     * even if JTAPI provider threads are still running (they are daemon-like
     * but not marked as daemon threads by Cisco's implementation).
     */
    public static void main(String[] args) {
        PhoneController controller = new PhoneController();
        controller.run(args);
        System.exit(0);
    }

    /**
     * Core initialization and execution lifecycle.
     *
     * <p>Sequence:</p>
     * <ol>
     *   <li>Parse command-line arguments (provider, credentials, device, DN, operation)</li>
     *   <li>Connect to CUCM JTAPI provider and wait for IN_SERVICE</li>
     *   <li>Resolve the target terminal (phone device) and address (directory number)</li>
     *   <li>Register observers for terminal, address, and (later) call events</li>
     *   <li>Read commands from stdin; if present, execute in command-driven mode</li>
     *   <li>If stdin is empty, fall back to legacy positional-argument mode</li>
     * </ol>
     *
     * <p>All exceptions are caught and printed as a JSON failure object on stdout.
     * Provider/observer cleanup always runs in the finally block.</p>
     *
     * @param args command-line arguments: provider username password deviceName directoryNumber operation [legacy-args...]
     */
    private void run(String[] args) {
        if (args.length < 6) {
            printFailure("Usage: <provider> <username> <password> <deviceName> <directoryNumber> <operation> [legacy-args...]");
            return;
        }

        try {
            String providerHost = args[0];
            String username = args[1];
            String password = args[2];
            String deviceName = args[3];
            String directoryNumber = args[4];
            String operation = args[5];

            // ── Connect provider ─────────────────────────────────────────
            // The provider string uses Cisco's semicolon-delimited format:
            //   <host>;login=<user>;passwd=<pass>
            // addObserver registers us for providerChangedEvent callbacks.
            // We block on the providerInService latch until we get ProvInServiceEv.
            JtapiPeer peer = JtapiPeerFactory.getJtapiPeer(null);
            String providerString = providerHost + ";login=" + username + ";passwd=" + password;
            debug("Opening provider", providerHost + ";login=" + username + ";passwd=***");
            this.provider = peer.getProvider(providerString);
            this.provider.addObserver(this);
            if (!providerInService.await(60, TimeUnit.SECONDS)) {
                printFailure("Provider did not reach IN_SERVICE within 60 seconds");
                return;
            }

            // ── Resolve terminal and address ─────────────────────────────
            // Terminal = the physical phone device (identified by SEP MAC).
            // Address  = the directory number associated with the line on that phone.
            // We add observers to both so we can track their lifecycle events.
            // emitDiagnostics() records detailed state info for evidence capture.
            debug("Resolving terminal", deviceName);
            this.terminal = provider.getTerminal(deviceName);
            if (this.terminal == null) {
                printFailure("Provider returned null terminal for device " + deviceName);
                return;
            }
            debug("Resolving address", directoryNumber);
            this.address = resolveAddress(provider, terminal, directoryNumber);
            if (this.address == null) {
                printFailure("Unable to resolve an in-service terminal address for directory number " + directoryNumber);
                return;
            }
            this.terminal.addObserver(this);
            this.address.addObserver(this);
            recordEvent("provider-opened", provider.getName());
            emitDiagnostics();
            recordState("IDLE");

            // ── Read commands from stdin ──────────────────────────────────
            // If the Python wrapper piped commands via stdin, we enter
            // command-driven mode.  Otherwise, we dispatch based on the
            // positional 'operation' argument for backward compatibility.
            List<String[]> commands = readCommands();

            if (!commands.isEmpty()) {
                // Command-driven mode
                executeCommands(commands, deviceName, directoryNumber);
            } else {
                // Legacy mode: fall back to operation-based dispatch
                executeLegacyOperation(operation, args, deviceName, directoryNumber);
            }

        } catch (Exception exc) {
            debug("Failure", exc.getClass().getSimpleName() + ": " + exc.getMessage());
            printFailure(exc.getClass().getSimpleName() + ": " + exc.getMessage());
        } finally {
            cleanup();
        }
    }

    // ── Command reading ──────────────────────────────────────────────────

    /**
     * Reads all commands from stdin in a non-blocking fashion.
     *
    * <p>The Python wrapper ({@code _runner.py}) pipes commands via
     * the subprocess stdin.  Each line is one command.  Lines starting with
     * {@code #} and blank lines are ignored (allows inline comments).</p>
     *
     * <p>If stdin has no data available (legacy invocation), returns an empty
     * list, which triggers legacy mode in {@link #run(String[])}.</p>
     *
     * @return ordered list of tokenized commands; empty if stdin has no data
     */
    private List<String[]> readCommands() {
        List<String[]> commands = new ArrayList<>();
        try {
            // Check if stdin has data available (non-blocking)
            if (System.in.available() <= 0) {
                return commands;
            }
            BufferedReader reader = new BufferedReader(new InputStreamReader(System.in));
            String line;
            while ((line = reader.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty() || line.startsWith("#")) {
                    continue;
                }
                commands.add(tokenizeLine(line));
            }
        } catch (Exception exc) {
            debug("Stdin read error (non-fatal)", exc.getMessage());
        }
        return commands;
    }

    /**
     * Tokenizes a single command line, splitting on whitespace while
     * respecting double-quoted values (e.g., {@code DIAL destination="91 800 555 1234"}).
     *
     * @param line raw command line from stdin
     * @return array of tokens where tokens[0] is the command verb
     */
    private String[] tokenizeLine(String line) {
        // Split on whitespace, respecting quoted values
        List<String> tokens = new ArrayList<>();
        StringBuilder current = new StringBuilder();
        boolean inQuotes = false;
        for (int i = 0; i < line.length(); i++) {
            char c = line.charAt(i);
            if (c == '"') {
                inQuotes = !inQuotes;
            } else if (c == ' ' && !inQuotes) {
                if (current.length() > 0) {
                    tokens.add(current.toString());
                    current.setLength(0);
                }
            } else {
                current.append(c);
            }
        }
        if (current.length() > 0) {
            tokens.add(current.toString());
        }
        return tokens.toArray(new String[0]);
    }

    /**
     * Extracts key=value parameters from tokenized command arguments.
     * Token 0 is the command verb and is skipped.  Parameters without
     * an {@code =} sign are ignored.
     *
     * @param tokens tokenized command line
     * @return ordered map of parameter key → value pairs
     */
    private Map<String, String> parseParams(String[] tokens) {
        Map<String, String> params = new LinkedHashMap<>();
        for (int i = 1; i < tokens.length; i++) {
            int eq = tokens[i].indexOf('=');
            if (eq > 0) {
                params.put(tokens[i].substring(0, eq), tokens[i].substring(eq + 1));
            }
        }
        return params;
    }

    // ── Command dispatch ─────────────────────────────────────────────────

    /**
     * Executes a sequence of commands read from stdin.
     *
     * <p>Registers a {@link CallControlCallObserver} on the address <b>before</b>
     * any call-related commands execute.  This is critical: the observer must
     * be in place before {@code connect()} or {@code answer()} so that
     * asynchronous call state events (TALKING, HELD, etc.) are captured.</p>
     *
     * <p>Commands execute sequentially.  If any command throws, the exception
     * propagates to {@link #run(String[])} which emits a failure JSON.</p>
     *
     * @param commands     ordered list of tokenized commands from stdin
     * @param deviceName   SEP device name for output labeling
     * @param directoryNumber  DN for output labeling
     */
    private void executeCommands(List<String[]> commands, String deviceName, String directoryNumber) throws Exception {
        // Register call observer upfront for all command-driven flows
        this.address.addCallObserver(this);
        recordEvent("call-observer-registered", directoryNumber);

        for (int i = 0; i < commands.size(); i++) {
            String[] tokens = commands.get(i);
            if (tokens.length == 0) continue;

            String cmd = tokens[0].toUpperCase();
            Map<String, String> params = parseParams(tokens);
            debug("Command " + (i + 1) + "/" + commands.size(), cmd + " " + params);

            switch (cmd) {
                case "DIAL":
                    cmdDial(params);
                    break;
                case "ANSWER":
                    cmdAnswer(params);
                    break;
                case "HOLD":
                    cmdHold(params);
                    break;
                case "RESUME":
                    cmdResume(params);
                    break;
                case "TRANSFER":
                    cmdTransfer(params);
                    break;
                case "CONFERENCE":
                    cmdConference(params);
                    break;
                case "SEND_DTMF":
                    cmdSendDtmf(params);
                    break;
                case "DISCONNECT":
                    cmdDisconnect(params);
                    break;
                case "SLEEP":
                    cmdSleep(params);
                    break;
                case "WAIT":
                    cmdWait(params);
                    break;
                case "INSPECT":
                    cmdInspect(params);
                    break;
                default:
                    printFailure("Unknown command: " + cmd);
                    return;
            }
        }

        String destination = "";
        for (String[] tokens : commands) {
            Map<String, String> p = parseParams(tokens);
            if ("DIAL".equalsIgnoreCase(tokens[0]) && p.containsKey("destination")) {
                destination = p.get("destination");
                break;
            }
        }
        printSuccess(deviceName, destination, commands.size());
    }

    // ── Command: DIAL ────────────────────────────────────────────────────

    /**
     * Initiates an outbound call from the controlled phone to a destination number.
     *
     * <p>Uses {@code Call.connect()} to place the call.  The connect() method may
     * throw a {@code PlatformExceptionImpl} due to Cisco's JTAPI timing quirks
     * (the call actually succeeded but the synchronous return timed out).
     * We catch this and fall through to the async TALKING event instead.</p>
     *
     * <p>Blocks until a TALKING event arrives on the {@link #stateEvents} queue,
     * confirming the far end answered.  Updates {@link #activeCall} and
     * {@link #activeTermConn} via the observer callback.</p>
     *
     * @param params must contain {@code destination}; optional {@code timeout} (default 60s)
     * @throws RuntimeException if no TALKING event within timeout
     */
    private void cmdDial(Map<String, String> params) throws Exception {
        String destination = requireParam(params, "destination", "DIAL");
        int timeout = intParam(params, "timeout", 60);

        recordAction("dial", destination);
        debug("Dialing", terminal.getName() + " -> " + destination + " via " + address.getName());

        Call call = provider.createCall();
        try {
            call.connect(terminal, address, destination);
            recordEvent("connect-returned", "synchronous");
        } catch (Exception exc) {
            String msg = exc.getClass().getSimpleName() + ": " + exc.getMessage();
            debug("connect() threw, waiting for async TALKING event", msg);
            recordEvent("connect-postcondition-timeout", msg);
        }

        // Wait for TALKING event
        if (!waitForStateEvent("TALKING", timeout)) {
            throw new RuntimeException("No TALKING event received within " + timeout + " seconds after DIAL");
        }
        activeCall.set(call);
        recordState("CONNECTED");
    }

    // ── Command: ANSWER ──────────────────────────────────────────────────

    /**
     * Waits for and answers an inbound call on the controlled phone.
     *
     * <p>Blocks until a RINGING event is deposited in the {@link #stateEvents}
     * queue (indicating an inbound call has arrived at the terminal).  Then
     * calls {@code TerminalConnection.answer()} and waits for TALKING to
     * confirm the call is connected.</p>
     *
     * @param params optional {@code timeout} (default 60s) for waiting for the inbound call
     * @throws RuntimeException if no inbound call arrives or TALKING is not reached
     */
    private void cmdAnswer(Map<String, String> params) throws Exception {
        int timeout = intParam(params, "timeout", 60);

        recordAction("answer", "waiting for inbound call");
        debug("Waiting for inbound call", terminal.getName() + " timeout=" + timeout);

        // Wait for RINGING event (inbound call arriving)
        if (!waitForStateEvent("RINGING", timeout)) {
            throw new RuntimeException("No inbound call received within " + timeout + " seconds");
        }

        // Answer the call
        CallControlTerminalConnection tc = activeTermConn.get();
        if (tc == null) {
            throw new RuntimeException("No terminal connection available to answer");
        }
        debug("Answering inbound call", terminal.getName());
        ((TerminalConnection) tc).answer();
        recordEvent("answer-sent", terminal.getName());

        // Wait for TALKING after answer
        if (!waitForStateEvent("TALKING", 30)) {
            throw new RuntimeException("No TALKING event received within 30 seconds after ANSWER");
        }
        recordState("CONNECTED");
    }

    // ── Command: HOLD ────────────────────────────────────────────────────

    /**
     * Places the current call on hold.
     *
     * <p>Calls {@code CallControlTerminalConnection.hold()} on the active
     * terminal connection and waits for a HELD event confirmation.</p>
     *
     * @param params optional {@code timeout} (default 30s)
     * @throws RuntimeException if no active terminal connection or HELD event not received
     */
    private void cmdHold(Map<String, String> params) throws Exception {
        int timeout = intParam(params, "timeout", 30);

        CallControlTerminalConnection tc = activeTermConn.get();
        if (tc == null) {
            throw new RuntimeException("No active terminal connection for HOLD");
        }

        recordAction("hold", null);
        tc.hold();

        if (!waitForStateEvent("HELD", timeout)) {
            throw new RuntimeException("No HELD event received within " + timeout + " seconds");
        }
        recordState("HELD");
    }

    // ── Command: RESUME ──────────────────────────────────────────────────

    /**
     * Resumes a held call.
     *
     * <p>Calls {@code CallControlTerminalConnection.unhold()} and waits for
     * a TALKING event to confirm the call is active again.</p>
     *
     * @param params optional {@code timeout} (default 30s)
     * @throws RuntimeException if no active terminal connection or TALKING event not received
     */
    private void cmdResume(Map<String, String> params) throws Exception {
        int timeout = intParam(params, "timeout", 30);

        CallControlTerminalConnection tc = activeTermConn.get();
        if (tc == null) {
            throw new RuntimeException("No active terminal connection for RESUME");
        }

        recordAction("resume", null);
        tc.unhold();

        if (!waitForStateEvent("TALKING", timeout)) {
            throw new RuntimeException("No TALKING event after RESUME within " + timeout + " seconds");
        }
        recordState("TALKING");
    }

    // ── Command: TRANSFER ────────────────────────────────────────────────

    /**
     * Transfers the current call to another destination.
     *
     * <p>Supports two transfer types:</p>
     * <ul>
     *   <li><b>blind</b> — Redirects the remote party's connection directly to the
     *       destination via {@code CallControlConnection.redirect()}.  Our leg
     *       drops immediately.</li>
     *   <li><b>consult</b> (default) — Places the current call on hold, initiates
     *       a consult call to the destination via {@code CallControlCall.consult()},
     *       waits for the consult party to answer (TALKING), then completes the
     *       transfer via {@code CallControlCall.transfer()}.  This creates a
     *       proper supervised transfer.</li>
     * </ul>
     *
     * @param params must contain {@code destination}; optional {@code type} (consult|blind),
     *               optional {@code timeout} (default 60s)
     * @throws RuntimeException if no active call or transfer fails
     */
    private void cmdTransfer(Map<String, String> params) throws Exception {
        String destination = requireParam(params, "destination", "TRANSFER");
        String type = params.getOrDefault("type", "consult");
        int timeout = intParam(params, "timeout", 60);

        recordAction("transfer", type + " -> " + destination);

        CallControlTerminalConnection tc = activeTermConn.get();
        if (tc == null) {
            throw new RuntimeException("No active terminal connection for TRANSFER");
        }

        if ("blind".equalsIgnoreCase(type)) {
            // Blind transfer: redirect the remote party connection
            debug("Blind transfer", destination);
            Call currentCall = activeCall.get();
            if (currentCall == null) {
                throw new RuntimeException("No active call for blind transfer");
            }
            Connection remoteConn = findRemoteConnection(currentCall);
            if (remoteConn == null) {
                throw new RuntimeException("No remote connection found for blind transfer");
            }
            ((CallControlConnection) remoteConn).redirect(destination);
            recordEvent("blind-transfer-sent", destination);

            if (!waitForStateEvent("DISCONNECTED", timeout)) {
                debug("Transfer may have completed without DISCONNECTED event", "");
            }
            recordState("TRANSFERRED");
        } else {
            // Consult transfer: hold, consult, wait for answer, complete
            debug("Consult transfer", destination);
            Call currentCall = activeCall.get();
            if (!(currentCall instanceof CallControlCall)) {
                throw new RuntimeException("Active call does not support CallControlCall for consult transfer");
            }
            CallControlCall ccCall = (CallControlCall) currentCall;

            // Initiate consult call
            Connection[] consultConns = ccCall.consult(tc, destination);
            recordEvent("consult-initiated", destination);

            if (consultConns == null || consultConns.length == 0) {
                throw new RuntimeException("consult() returned no connections");
            }
            Call consultCall = consultConns[0].getCall();
            debug("Consult call created", consultCall.toString());

            // Wait for consult party to answer
            if (!waitForStateEvent("TALKING", timeout)) {
                throw new RuntimeException("Consult call did not reach TALKING within " + timeout + " seconds");
            }
            recordEvent("consult-connected", destination);

            // Complete the transfer
            ccCall.transfer(consultCall);
            recordEvent("transfer-completed", destination);
            recordState("TRANSFERRED");

            // Clear active state since our leg is dropped
            activeTermConn.set(null);
            activeCall.set(null);
        }
    }

    // ── Command: CONFERENCE ──────────────────────────────────────────────

    /**
     * Adds a third party to the current call, creating a conference.
     *
     * <p>Uses the same consult pattern as transfer: the current call is held,
     * a consult leg is established to the new destination, and once the
     * consult party answers, {@code CallControlCall.conference()} merges
     * all legs into a single conference call.</p>
     *
     * @param params must contain {@code destination}; optional {@code timeout} (default 60s)
     * @throws RuntimeException if no active call or conference initiation fails
     */
    private void cmdConference(Map<String, String> params) throws Exception {
        String destination = requireParam(params, "destination", "CONFERENCE");
        int timeout = intParam(params, "timeout", 60);

        recordAction("conference", destination);

        CallControlTerminalConnection tc = activeTermConn.get();
        if (tc == null) {
            throw new RuntimeException("No active terminal connection for CONFERENCE");
        }
        Call currentCall = activeCall.get();
        if (!(currentCall instanceof CallControlCall)) {
            throw new RuntimeException("Active call does not support CallControlCall for conference");
        }
        CallControlCall ccCall = (CallControlCall) currentCall;

        debug("Conference consult", destination);
        Connection[] consultConns = ccCall.consult(tc, destination);
        recordEvent("conference-consult-initiated", destination);

        if (consultConns == null || consultConns.length == 0) {
            throw new RuntimeException("consult() returned no connections for conference");
        }
        Call consultCall = consultConns[0].getCall();

        // Wait for consult party to answer
        if (!waitForStateEvent("TALKING", timeout)) {
            throw new RuntimeException("Conference consult call did not reach TALKING within " + timeout + " seconds");
        }
        recordEvent("conference-consult-connected", destination);

        // Complete the conference
        ccCall.conference(consultCall);
        recordEvent("conference-completed", destination);
        recordState("CONFERENCED");
    }

    // ── Command: SEND_DTMF ──────────────────────────────────────────────

    /**
     * Sends DTMF tones on the active call.
     *
     * <p>Uses {@code MediaTerminalConnection.generateDtmf()} which is the
     * correct interface for DTMF generation in JTAPI.  Note: this is
     * <b>not</b> on {@code CiscoTerminalConnection} as one might expect
     * — it lives in the {@code javax.telephony.media} package.</p>
     *
     * @param params must contain {@code digits} (e.g., "1234#")
     * @throws RuntimeException if terminal connection doesn't support media operations
     */
    private void cmdSendDtmf(Map<String, String> params) throws Exception {
        String digits = requireParam(params, "digits", "SEND_DTMF");

        recordAction("send_dtmf", digits);

        CallControlTerminalConnection tc = activeTermConn.get();
        if (tc == null) {
            throw new RuntimeException("No active terminal connection for SEND_DTMF");
        }

        if (tc instanceof MediaTerminalConnection) {
            debug("Sending DTMF via MediaTerminalConnection", digits);
            ((MediaTerminalConnection) tc).generateDtmf(digits);
            recordEvent("dtmf-sent", digits);
        } else {
            throw new RuntimeException("Terminal connection does not support DTMF generation (not MediaTerminalConnection)");
        }
    }

    // ── Command: DISCONNECT ──────────────────────────────────────────────

    /**
     * Disconnects the active call.
     *
     * <p>Attempts to disconnect using the controlled phone's own connection
     * first (found by matching address name).  If that fails, falls back to
     * disconnecting via the terminal connection.  Clears the active call and
     * terminal connection references after disconnect.</p>
     *
     * @param params (none required)
     */
    private void cmdDisconnect(Map<String, String> params) throws Exception {
        recordAction("release", null);
        Exception disconnectError = null;

        // Try active call's source connection first
        Call call = activeCall.get();
        if (call != null) {
            Connection[] conns = call.getConnections();
            if (conns != null) {
                for (Connection conn : conns) {
                    if (conn != null && conn.getAddress() != null
                            && conn.getAddress().getName().equals(address.getName())) {
                        try {
                            conn.disconnect();
                            recordState("DISCONNECTED");
                            activeTermConn.set(null);
                            activeCall.set(null);
                            return;
                        } catch (Exception exc) {
                            disconnectError = exc;
                            recordEvent("disconnect-source-failed", exc.getClass().getSimpleName() + ": " + exc.getMessage());
                        }
                    }
                }
            }
        }

        // Fallback: disconnect via terminal connection
        CallControlTerminalConnection tc = activeTermConn.get();
        if (tc != null) {
            try {
                tc.getConnection().disconnect();
                recordState("DISCONNECTED");
                activeTermConn.set(null);
                activeCall.set(null);
                return;
            } catch (Exception exc) {
                disconnectError = exc;
                recordEvent("disconnect-terminal-failed", exc.getClass().getSimpleName() + ": " + exc.getMessage());
            }
        }

        if (disconnectError != null) {
            debug("Disconnect already cleared or unavailable", disconnectError.getClass().getSimpleName() + ": " + disconnectError.getMessage());
        } else {
            debug("No connection to disconnect", "");
        }
        recordState("DISCONNECTED");
        activeTermConn.set(null);
        activeCall.set(null);
    }

    // ── Command: SLEEP ───────────────────────────────────────────────────

    /**
     * Pauses execution for a specified duration.
     *
     * <p>Used between call control operations to allow the call to remain
     * in a given state for a measurable period (e.g., hold for 15 seconds
     * to verify MOH, or stay connected for a 2-hour duration test).</p>
     *
     * @param params optional {@code seconds} (default 1)
     */
    private void cmdSleep(Map<String, String> params) throws InterruptedException {
        int seconds = intParam(params, "seconds", 1);
        if (seconds <= 0) return;
        recordAction("sleep", Integer.toString(seconds));
        debug("Sleeping", seconds + "s");
        Thread.sleep(TimeUnit.SECONDS.toMillis(seconds));
    }

    // ── Command: WAIT ────────────────────────────────────────────────────

    /**
     * Blocks until a specific state event is received from the observer.
     *
     * <p>This is a generic wait command that can pause execution until
     * any recognized state event (TALKING, HELD, RINGING, DISCONNECTED,
     * ALERTING) arrives.  Useful for synchronizing with external actions
     * (e.g., waiting for a remote party to hang up).</p>
     *
     * @param params must contain {@code state}; optional {@code timeout} (default 60s)
     * @throws RuntimeException if the expected state is not reached within timeout
     */
    private void cmdWait(Map<String, String> params) throws Exception {
        String state = requireParam(params, "state", "WAIT").toUpperCase();
        int timeout = intParam(params, "timeout", 60);

        recordAction("wait", state + " timeout=" + timeout);
        if (!waitForStateEvent(state, timeout)) {
            throw new RuntimeException("Timed out waiting for state " + state + " within " + timeout + " seconds");
        }
        recordState(state);
    }

    // ── Command: INSPECT ─────────────────────────────────────────────────

    /**
     * Inspects the current state of the terminal and address without making calls.
     *
     * <p>If the address is a {@link CiscoAddress} that is not yet IN_SERVICE,
     * waits up to {@code address_timeout} seconds for it to register.
     * Emits full diagnostics (terminal state, registration, address type,
     * partition, etc.) and records the results as events.</p>
     *
     * <p>This command is used by the test framework to verify phone
     * connectivity and registration before running call flow tests.</p>
     *
     * @param params optional {@code address_timeout} (default 15s)
     */
    private void cmdInspect(Map<String, String> params) throws Exception {
        int addressTimeout = intParam(params, "address_timeout", 15);

        recordAction("inspect", terminal.getName());

        // Wait for address IN_SERVICE if needed
        if (address instanceof CiscoAddress) {
            CiscoAddress ca = (CiscoAddress) address;
            if (ca.getState() == CiscoAddress.IN_SERVICE) {
                recordEvent("address-already-in-service", "true");
            } else {
                debug("Address OUT_OF_SERVICE, waiting", addressTimeout + "s");
                recordEvent("address-wait-started", "waiting up to " + addressTimeout + "s for IN_SERVICE");
                boolean ready = addressInService.await(addressTimeout, TimeUnit.SECONDS);
                recordEvent("address-wait-result", ready ? "IN_SERVICE" : "TIMED_OUT_STILL_OUT_OF_SERVICE");
            }
        }

        emitDiagnostics();
        recordEvent("inspect-complete", terminal.getName());
    }

    // ── Event waiting ────────────────────────────────────────────────────

    /**
     * Blocks the calling thread until the expected state event appears in
     * the {@link #stateEvents} queue, or until the timeout expires.
     *
     * <p>Non-matching events are consumed and logged but do not reset the
     * timeout.  This is intentional: if multiple events fire in rapid
     * succession (e.g., ALERTING then TALKING), we need to consume each
     * in order and only return when the expected one arrives.</p>
     *
     * @param expected       state event to wait for (e.g., "TALKING", "HELD")
     * @param timeoutSeconds maximum seconds to wait
     * @return true if the expected event was received, false on timeout
     */
    private boolean waitForStateEvent(String expected, int timeoutSeconds) throws InterruptedException {
        long deadline = System.currentTimeMillis() + TimeUnit.SECONDS.toMillis(timeoutSeconds);
        while (System.currentTimeMillis() < deadline) {
            long remaining = deadline - System.currentTimeMillis();
            if (remaining <= 0) break;
            String event = stateEvents.poll(remaining, TimeUnit.MILLISECONDS);
            if (event == null) break;

            debug("State event received", event + " (waiting for " + expected + ")");
            if (event.equals(expected)) {
                return true;
            }
            // Non-matching events are logged but consumed
            recordEvent("state-event-skipped", event + " (expected " + expected + ")");
        }
        return false;
    }

    // ── Legacy operation support ─────────────────────────────────────────

    /**
     * Executes a predefined call flow based on the legacy positional operation argument.
     *
     * <p>Supported legacy operations:</p>
     * <ul>
     *   <li>{@code inspect_terminal} — checks phone registration and emits diagnostics</li>
     *   <li>{@code outbound_call_flow} — dials, waits, holds, waits, resumes, waits,
     *       then disconnects (the original automated test pattern)</li>
     * </ul>
     *
     * <p>This mode is retained for backward compatibility with earlier test scripts
    * (e.g., Support and Diagnostics/jtapi-8875-outbound-call.py) that pass positional arguments rather
     * than piping commands via stdin.</p>
     *
     * @param operation        operation name from args[5]
     * @param args             full command-line arguments (for positional param extraction)
     * @param deviceName       SEP device name for output
     * @param directoryNumber  DN for output
     */
    private void executeLegacyOperation(String operation, String[] args, String deviceName, String directoryNumber) throws Exception {
        if ("inspect_terminal".equals(operation)) {
            Map<String, String> inspectParams = new HashMap<>();
            inspectParams.put("address_timeout", "15");
            cmdInspect(inspectParams);
            printInspectionSuccess(deviceName, directoryNumber);
            return;
        }

        if ("outbound_call_flow".equals(operation)) {
            // Parse legacy positional args
            String destination = args.length > 6 ? args[6] : "";
            int establishedSeconds = args.length > 7 ? Integer.parseInt(args[7]) : 30;
            int holdSeconds = args.length > 8 ? Integer.parseInt(args[8]) : 30;
            int postResumeSeconds = args.length > 9 ? Integer.parseInt(args[9]) : 30;
            boolean autoRelease = args.length > 10 ? Boolean.parseBoolean(args[10]) : true;

            // Register call observer
            this.address.addCallObserver(this);
            recordEvent("call-observer-registered", directoryNumber);

            Map<String, String> dialParams = new HashMap<>();
            dialParams.put("destination", destination);
            cmdDial(dialParams);

            cmdSleep(mapOf("seconds", Integer.toString(establishedSeconds)));
            cmdHold(new HashMap<>());
            cmdSleep(mapOf("seconds", Integer.toString(holdSeconds)));
            cmdResume(new HashMap<>());
            cmdSleep(mapOf("seconds", Integer.toString(postResumeSeconds)));

            if (autoRelease) {
                cmdDisconnect(new HashMap<>());
            }

            printSuccess(deviceName, destination, 0);
            return;
        }

        printFailure("Unknown operation: " + operation);
    }

    // ── Observer: Provider ───────────────────────────────────────────────

    /**
     * Callback from the JTAPI provider when its state changes.
     *
     * <p>We only care about {@link ProvInServiceEv} which signals that the
     * CTI connection to CUCM is fully established and we can proceed with
     * terminal/address resolution.  Releases the {@link #providerInService} latch.</p>
     */
    @Override
    public void providerChangedEvent(ProvEv[] eventList) {
        if (eventList == null) return;
        for (ProvEv event : eventList) {
            if (event instanceof ProvInServiceEv) {
                providerInService.countDown();
                debug("Provider event", "IN_SERVICE");
                recordEvent("provider-in-service", event.getProvider().getName());
            }
        }
    }

    // ── Observer: Terminal ────────────────────────────────────────────────

    /**
     * Callback from the JTAPI provider when terminal (device) events occur.
     *
     * <p>Currently used for diagnostic logging only.  Terminal events include
     * registration state changes, but we rely on the address observer for
     * IN_SERVICE detection since address state is what matters for call control.</p>
     */
    @Override
    public void terminalChangedEvent(TermEv[] eventList) {
        if (eventList == null) return;
        for (TermEv event : eventList) {
            if (event.getTerminal() != null) {
                debug("Terminal event", event.getTerminal().getName() + ":" + event.getID());
                recordEvent("terminal-event", event.getTerminal().getName() + ":" + event.getID());
            }
        }
    }

    // ── Observer: Address ────────────────────────────────────────────────

    /**
     * Callback from the JTAPI provider when address (directory number) events occur.
     *
     * <p>The critical event here is {@code CiscoAddrInServiceEv} which signals
     * that the directory number is registered and ready for call control.
     * Note: we must check for the <b>Cisco extension</b> event class
     * ({@code com.cisco.jtapi.extensions.CiscoAddrInServiceEv}), not the
     * standard JTAPI {@code javax.telephony.events.AddrInServiceEv}, which
     * Cisco's implementation does not fire.</p>
     */
    @Override
    public void addressChangedEvent(AddrEv[] eventList) {
        if (eventList == null) return;
        for (AddrEv event : eventList) {
            String addrName = (event.getAddress() != null) ? event.getAddress().getName() : "unknown";
            debug("Address event", addrName + ":" + event.getID());
            recordEvent("address-observer-event", addrName + ":" + event.getID());
            if (event instanceof com.cisco.jtapi.extensions.CiscoAddrInServiceEv) {
                debug("Address IN_SERVICE detected", addrName);
                addressInService.countDown();
            }
        }
    }

    // ── Observer: Call (CallControlCallObserver) ──────────────────────────

    /**
     * Callback from the JTAPI provider when call state events occur.
     *
     * <p>This is the <b>most critical observer</b> for call control automation.
     * It receives events from the {@link CallControlCallObserver} interface
     * registered on the address.  Events handled:</p>
     *
     * <ul>
     *   <li>{@code CallCtlTermConnTalkingEv} → deposits "TALKING" into the queue.
     *       Fired when the terminal connection enters the talking state
     *       (call connected and audio path established).</li>
     *   <li>{@code TermConnActiveEv} → deposits "TALKING" (base JTAPI fallback).
     *       Some CUCM versions fire this instead of the CallControl variant.</li>
     *   <li>{@code CallCtlTermConnHeldEv} → deposits "HELD".
     *       Fired when the terminal connection enters the held state.</li>
     *   <li>{@code TermConnRingingEv} / {@code CallCtlTermConnRingingEv} → deposits "RINGING".
     *       Fired when an inbound call arrives at the terminal.</li>
     *   <li>{@code ConnDisconnectedEv} → deposits "DISCONNECTED".
     *       Fired when any connection in the call disconnects.</li>
     *   <li>{@code CallCtlConnAlertingEv} → deposits "ALERTING".
     *       Fired when the remote party's phone is ringing (outbound alerting).</li>
     * </ul>
     *
     * <p>Each event also updates {@link #activeTermConn} and {@link #activeCall}
     * so that subsequent commands can operate on the live call objects.
     * Thread safety is provided by {@link AtomicReference}.</p>
     */
    @Override
    public void callChangedEvent(CallEv[] eventList) {
        if (eventList == null) return;
        for (CallEv event : eventList) {
            recordEvent("call-observer-event", event.getClass().getSimpleName() + ":" + event.getID());

            // CallControl TALKING: primary indicator that audio path is up
            if (event instanceof CallCtlTermConnTalkingEv) {
                CallCtlTermConnTalkingEv talkEv = (CallCtlTermConnTalkingEv) event;
                TerminalConnection tc = talkEv.getTerminalConnection();
                if (tc instanceof CallControlTerminalConnection) {
                    activeTermConn.set((CallControlTerminalConnection) tc);
                    if (tc.getConnection() != null && tc.getConnection().getCall() != null) {
                        activeCall.set(tc.getConnection().getCall());
                    }
                    debug("TALKING event", tc.getTerminal().getName());
                }
                stateEvents.offer("TALKING");
            // Base JTAPI ACTIVE: fallback for CUCM versions that don't fire
            // the CallControl variant.  Maps to TALKING for consistency.
            } else if (event instanceof TermConnActiveEv) {
                TermConnActiveEv activeEv = (TermConnActiveEv) event;
                TerminalConnection tc = activeEv.getTerminalConnection();
                if (tc instanceof CallControlTerminalConnection) {
                    activeTermConn.set((CallControlTerminalConnection) tc);
                    if (tc.getConnection() != null && tc.getConnection().getCall() != null) {
                        activeCall.set(tc.getConnection().getCall());
                    }
                    debug("ACTIVE event (base JTAPI)", tc.getTerminal().getName());
                }
                stateEvents.offer("TALKING");
            } else if (event instanceof CallCtlTermConnHeldEv) {
                CallCtlTermConnHeldEv holdEv = (CallCtlTermConnHeldEv) event;
                TerminalConnection tc = holdEv.getTerminalConnection();
                if (tc instanceof CallControlTerminalConnection) {
                    activeTermConn.set((CallControlTerminalConnection) tc);
                    debug("HELD event", tc.getTerminal().getName());
                }
                stateEvents.offer("HELD");
            // RINGING: handles both base JTAPI and CallControl variants
            // to ensure inbound calls are detected regardless of event type
            } else if (event instanceof TermConnRingingEv || event instanceof CallCtlTermConnRingingEv) {
                TerminalConnection tc = null;
                if (event instanceof TermConnRingingEv) {
                    tc = ((TermConnRingingEv) event).getTerminalConnection();
                } else {
                    tc = ((CallCtlTermConnRingingEv) event).getTerminalConnection();
                }
                if (tc instanceof CallControlTerminalConnection) {
                    activeTermConn.set((CallControlTerminalConnection) tc);
                    if (tc.getConnection() != null && tc.getConnection().getCall() != null) {
                        activeCall.set(tc.getConnection().getCall());
                    }
                    debug("RINGING event", tc.getTerminal().getName());
                }
                stateEvents.offer("RINGING");
            } else if (event instanceof ConnDisconnectedEv) {
                stateEvents.offer("DISCONNECTED");
                debug("DISCONNECTED event", "");
            } else if (event instanceof CallCtlConnAlertingEv) {
                stateEvents.offer("ALERTING");
                debug("ALERTING event (remote party ringing)", "");
            }
        }
    }

    // ── JTAPI helpers ────────────────────────────────────────────────────

    /**
     * Resolves the JTAPI Address object for the given directory number.
     *
     * <p>Resolution strategy (in order):</p>
     * <ol>
     *   <li>Check terminal's associated addresses for an exact DN match</li>
     *   <li>Fall back to the first non-null terminal address (if DN not found)</li>
     *   <li>Fall back to provider-level address lookup by DN</li>
     * </ol>
     *
     * <p>The terminal-first approach is preferred because it guarantees the
     * address is actually associated with the physical phone we're controlling.</p>
     *
     * @param provider         JTAPI provider for fallback lookup
     * @param terminal         target phone terminal
     * @param directoryNumber  directory number to resolve
     * @return the resolved Address, or null if not found
     */
    private Address resolveAddress(Provider provider, Terminal terminal, String directoryNumber) throws Exception {
        Address[] terminalAddresses = terminal.getAddresses();
        if (terminalAddresses != null && terminalAddresses.length > 0) {
            debug("Terminal addresses", joinAddressNames(terminalAddresses));
            for (Address candidate : terminalAddresses) {
                if (candidate != null && directoryNumber.equals(candidate.getName())) {
                    debug("Using terminal address", candidate.getName());
                    return candidate;
                }
            }
            for (Address candidate : terminalAddresses) {
                if (candidate != null) {
                    debug("Using first terminal address", candidate.getName());
                    return candidate;
                }
            }
        }
        Address providerAddress = provider.getAddress(directoryNumber);
        if (providerAddress != null) {
            debug("Using provider address", providerAddress.getName());
        }
        return providerAddress;
    }

    /**
     * Finds the remote party's connection in a call (i.e., the connection
     * that does NOT belong to our controlled address).  Used by blind
     * transfer to redirect the remote party.
     *
     * @param call the active call to search
     * @return the remote connection, or null if not found
     */
    private Connection findRemoteConnection(Call call) {
        Connection[] conns = call.getConnections();
        if (conns == null) return null;
        for (Connection conn : conns) {
            if (conn != null && conn.getAddress() != null
                    && !conn.getAddress().getName().equals(address.getName())) {
                return conn;
            }
        }
        return null;
    }

    /**
     * Removes observers during shutdown.  Called in the finally block of
     * {@link #run(String[])} to ensure clean teardown regardless of outcome.
     * Exceptions during cleanup are silently swallowed so shutdown cannot
     * mask the command result already recorded for stdout.
     */
    private void cleanup() {
        if (address != null) {
            try { address.removeCallObserver(this); } catch (Exception ignored) {}
            try { address.removeObserver(this); } catch (Exception ignored) {}
        }
        if (terminal != null) {
            try { terminal.removeObserver(this); } catch (Exception ignored) {}
        }
        if (provider != null) {
            try { provider.shutdown(); } catch (Exception ignored) {}
        }
    }

    // ── Diagnostics ──────────────────────────────────────────────────────

    private void emitDiagnostics() {
        recordEvent("provider-state", describeProviderState(provider.getState()));
        recordEvent("terminal-class", terminal.getClass().getName());
        recordEvent("address-class", address.getClass().getName());

        try {
            Address providerAddress = provider.getAddress(address.getName());
            if (providerAddress != null) {
                recordEvent("provider-address-class", providerAddress.getClass().getName());
                recordEvent("provider-address-match", Boolean.toString(providerAddress == address));
                if (providerAddress instanceof CiscoAddress) {
                    recordEvent("provider-address-diagnostics", describeCiscoAddress((CiscoAddress) providerAddress));
                }
            }
        } catch (Exception exc) {
            recordEvent("provider-address-diagnostics-error", exc.getClass().getSimpleName() + ": " + exc.getMessage());
        }

        if (terminal instanceof CiscoTerminal) {
            recordEvent("terminal-diagnostics", describeCiscoTerminal((CiscoTerminal) terminal));
        }
        if (address instanceof CiscoAddress) {
            recordEvent("address-diagnostics", describeCiscoAddress((CiscoAddress) address));
        }
    }

    /**
     * Builds a diagnostic string describing a Cisco terminal's state.
     * Uses deprecated Cisco APIs (getRegistrationState, getDeviceState,
     * isRegistered, etc.) that are still functional on CUCM 15.x.
     */
    @SuppressWarnings("deprecation")
    private String describeCiscoTerminal(CiscoTerminal term) {
        try {
            return "name=" + term.getName()
                + ",state=" + describeCiscoTerminalState(term.getState())
                + ",registration=" + describeCiscoTerminalState(term.getRegistrationState())
                + ",deviceState=" + describeCiscoDeviceState(term.getDeviceState())
                + ",registered=" + term.isRegistered()
                + ",restricted=" + term.isRestricted()
                + ",loginType=" + describeCiscoLoginType(term.getLoginType())
                + ",protocol=" + term.getProtocol();
        } catch (Exception exc) {
            return "error=" + exc.getClass().getSimpleName() + ": " + exc.getMessage();
        }
    }

    /**
     * Builds a diagnostic string describing a Cisco address's state.
     * Includes registration, type, partition, and restriction details
     * relevant for troubleshooting DN registration issues.
     */
    @SuppressWarnings("deprecation")
    private String describeCiscoAddress(CiscoAddress addr) {
        try {
            return "name=" + addr.getName()
                + ",state=" + describeCiscoAddressState(addr.getState())
                + ",registration=" + describeCiscoAddressState(addr.getRegistrationState())
                + ",type=" + describeCiscoAddressType(addr.getType())
                + ",partition=" + nullToEmpty(addr.getPartition())
                + ",restrictedOnTerminal=" + addr.isRestricted(terminal)
                + ",inServiceTerminals=" + safeLength(addr.getInServiceAddrTerminals())
                + ",restrictedTerminals=" + safeLength(addr.getRestrictedAddrTerminals());
        } catch (Exception exc) {
            return "error=" + exc.getClass().getSimpleName() + ": " + exc.getMessage();
        }
    }

    // ── State description helpers ──────────────────────────────────────
    // These methods convert JTAPI integer state constants into human-readable
    // strings for diagnostic output.  Each maps the known constant values
    // defined in Cisco's JTAPI interfaces to their symbolic names.──

    private String describeProviderState(int state) {
        switch (state) {
            case Provider.IN_SERVICE: return "IN_SERVICE";
            case Provider.OUT_OF_SERVICE: return "OUT_OF_SERVICE";
            case Provider.SHUTDOWN: return "SHUTDOWN";
            default: return Integer.toString(state);
        }
    }

    private String describeCiscoTerminalState(int state) {
        switch (state) {
            case CiscoTerminal.IN_SERVICE: return "IN_SERVICE";
            case CiscoTerminal.OUT_OF_SERVICE: return "OUT_OF_SERVICE";
            default: return Integer.toString(state);
        }
    }

    private String describeCiscoDeviceState(int state) {
        switch (state) {
            case CiscoTerminal.DEVICESTATE_IDLE: return "IDLE";
            case CiscoTerminal.DEVICESTATE_ACTIVE: return "ACTIVE";
            case CiscoTerminal.DEVICESTATE_ALERTING: return "ALERTING";
            case CiscoTerminal.DEVICESTATE_HELD: return "HELD";
            case CiscoTerminal.DEVICESTATE_WHISPER: return "WHISPER";
            case CiscoTerminal.DEVICESTATE_UNKNOWN: return "UNKNOWN";
            default: return Integer.toString(state);
        }
    }

    private String describeCiscoLoginType(int loginType) {
        switch (loginType) {
            case CiscoTerminal.NO_LOGIN: return "NO_LOGIN";
            case CiscoTerminal.NATIVE_LOGIN: return "NATIVE_LOGIN";
            case CiscoTerminal.VISITOR_LOGIN: return "VISITOR_LOGIN";
            default: return Integer.toString(loginType);
        }
    }

    private String describeCiscoAddressState(int state) {
        switch (state) {
            case CiscoAddress.IN_SERVICE: return "IN_SERVICE";
            case CiscoAddress.OUT_OF_SERVICE: return "OUT_OF_SERVICE";
            default: return Integer.toString(state);
        }
    }

    private String describeCiscoAddressType(int type) {
        switch (type) {
            case CiscoAddress.INTERNAL: return "INTERNAL";
            case CiscoAddress.EXTERNAL: return "EXTERNAL";
            case CiscoAddress.EXTERNAL_UNKNOWN: return "EXTERNAL_UNKNOWN";
            case CiscoAddress.UNKNOWN: return "UNKNOWN";
            case CiscoAddress.MONITORING_TARGET: return "MONITORING_TARGET";
            case CiscoAddress.HUNT_PILOT: return "HUNT_PILOT";
            default: return Integer.toString(type);
        }
    }

    // ── Output helpers ───────────────────────────────────────────────────
    // Utility methods for formatting and debugging.

    /** Joins an array of Address objects into a comma-separated name string. */

    private String joinAddressNames(Address[] addresses) throws Exception {
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < addresses.length; i++) {
            if (i > 0) b.append(',');
            b.append(addresses[i] == null ? "<null>" : addresses[i].getName());
        }
        return b.toString();
    }

    /** Null-safe array length. Returns 0 for null arrays. */
    private int safeLength(Object[] values) { return values == null ? 0 : values.length; }

    /** Null-safe string coercion. Returns empty string for null input. */
    private String nullToEmpty(String v) { return v == null ? "" : v; }

    /**
     * Emits a timestamped debug message to stderr.
     * All debug output goes to stderr so it doesn't contaminate the JSON
     * result on stdout.  The Python wrapper captures stderr separately
     * for diagnostic logging.
     */

    private void debug(String stage, String detail) {
        System.err.println(Instant.now() + " [JTAPI] " + stage + ": " + detail);
        System.err.flush();
    }

    // ── Param parsing helpers ──────────────────────────────────────────
    // These extract and validate command parameters from the key=value
    // maps produced by parseParams().

    /** Extracts a required parameter or throws with a descriptive error message. */
    private String requireParam(Map<String, String> params, String key, String command) {
        String value = params.get(key);
        if (value == null || value.isEmpty()) {
            throw new IllegalArgumentException(command + " requires " + key + " parameter");
        }
        return value;
    }

    /** Extracts an optional integer parameter with a default value. */
    private int intParam(Map<String, String> params, String key, int defaultValue) {
        String value = params.get(key);
        if (value == null || value.isEmpty()) return defaultValue;
        return Integer.parseInt(value);
    }

    /** Convenience factory for single-entry parameter maps (used by legacy mode). */
    private Map<String, String> mapOf(String key, String value) {
        Map<String, String> m = new HashMap<>();
        m.put(key, value);
        return m;
    }

    // ── JSON recording and output ──────────────────────────────────────
    // JSON is built manually (no external dependencies like Gson/Jackson)
    // to keep the runtime self-contained with only the Cisco JTAPI jars.
    // Each record* method builds a JSON fragment string with an ISO-8601
    // timestamp and appends it to the appropriate list.  The print* methods
    // assemble the final output object wrapping all three lists.

    /** Records a user-initiated action (e.g., dial, hold, disconnect). */
    private void recordAction(String action, String detail) {
        StringBuilder b = new StringBuilder();
        b.append("{");
        appendField(b, "at", Instant.now().toString(), true);
        appendField(b, "action", action, false);
        if (detail != null) appendField(b, "detail", detail, false);
        b.append("}");
        actionEntries.add(b.toString());
    }

    /** Records a phone state transition (e.g., IDLE, CONNECTED, HELD, DISCONNECTED). */
    private void recordState(String state) {
        StringBuilder b = new StringBuilder();
        b.append("{");
        appendField(b, "at", Instant.now().toString(), true);
        appendField(b, "state", state, false);
        b.append("}");
        stateEntries.add(b.toString());
    }

    /** Records a raw JTAPI event with detail for the diagnostic audit trail. */
    private void recordEvent(String event, String detail) {
        StringBuilder b = new StringBuilder();
        b.append("{");
        appendField(b, "at", Instant.now().toString(), true);
        appendField(b, "event", event, false);
        appendField(b, "detail", detail, false);
        b.append("}");
        eventEntries.add(b.toString());
    }

    /**
     * Emits the final success JSON to stdout after command-driven execution.
     * Includes all accumulated actions, states, and events as arrays.
     */
    private void printSuccess(String deviceName, String destination, int commandCount) {
        StringBuilder b = new StringBuilder();
        b.append("{");
        appendField(b, "status", "completed", true);
        if (commandCount > 0) {
            appendField(b, "message", "JTAPI completed " + commandCount + " commands on " + deviceName, false);
        } else {
            appendField(b, "message", "JTAPI completed scripted outbound call flow to " + destination + " for " + deviceName, false);
        }
        appendField(b, "call_id", "live-" + Instant.now().toEpochMilli(), false);
        appendArrayField(b, "actions", actionEntries);
        appendArrayField(b, "states", stateEntries);
        appendArrayField(b, "events", eventEntries);
        b.append("}");
        System.out.print(b);
    }

    /** Emits the final success JSON for inspect_terminal operations. */
    private void printInspectionSuccess(String deviceName, String directoryNumber) {
        StringBuilder b = new StringBuilder();
        b.append("{");
        appendField(b, "status", "completed", true);
        appendField(b, "message", "JTAPI inspection completed for " + deviceName + " / " + directoryNumber, false);
        appendArrayField(b, "actions", actionEntries);
        appendArrayField(b, "states", stateEntries);
        appendArrayField(b, "events", eventEntries);
        b.append("}");
        System.out.print(b);
    }

    /** Emits a failure JSON to stdout with the error message and any partial data. */
    private void printFailure(String error) {
        StringBuilder b = new StringBuilder();
        b.append("{");
        appendField(b, "status", "failed", true);
        appendField(b, "error", error, false);
        appendArrayField(b, "actions", actionEntries);
        appendArrayField(b, "states", stateEntries);
        appendArrayField(b, "events", eventEntries);
        b.append("}");
        System.out.print(b);
    }

    /** Appends a JSON key-value string field.  Uses 'first' to control comma placement. */
    private void appendField(StringBuilder b, String key, String value, boolean first) {
        if (!first) b.append(',');
        b.append('"').append(escape(key)).append('"').append(':').append('"').append(escape(value)).append('"');
    }

    /** Appends a JSON array field containing pre-built JSON object strings. */
    private void appendArrayField(StringBuilder b, String key, List<String> entries) {
        b.append(',').append('"').append(escape(key)).append('"').append(':').append('[');
        for (int i = 0; i < entries.size(); i++) {
            if (i > 0) b.append(',');
            b.append(entries.get(i));
        }
        b.append(']');
    }

    /** Escapes special characters for safe JSON string embedding. */
    private String escape(String value) {
        return value.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n").replace("\r", "\\r");
    }
}
