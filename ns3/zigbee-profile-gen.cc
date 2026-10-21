/*
 * zigbee-profile-gen.cc
 *
 * Scenariusz ns-3 generujący parametry sieci (opóźnienie, jitter, utrata pakietów per urządzenie)
 * dla symulatora urządzeń Zigbee z sim/network_profile.json.
 *
 * Co robi:
 *   1. Wczytuje topologię z pliku CSV (topology.csv): węzły i tłumienia łączy w dB.
 *   2. Buduje sieć IEEE 802.15.4 (lr-wpan) z macierzą tłumień: słyszą się tylko węzły połączone linkiem.
 *   3. Tworzy sieć Zigbee (warstwa NWK): koordynator formuje sieć, pozostałe węzły dołączają
 *      kolejno (asocjacja MAC) w kolejności od koordynatora.
 *   4. Wysyła sondy NLDE-DATA: w górę (urządzenie -> koordynator) i w dół (koordynator -> urządzenie),
 *      z wykrywaniem tras (AODV) i mierzy opóźnienie oraz utratę pakietów.
 *   5. Zapisuje wynik per sonda do CSV (przetwarzany przez ns3_to_profile.py).
 *
 * Testowane z ns-3 3.44 (moduł Zigbee zawierający tylko warstwę NWK). W nowszych wydaniach moduł
 * ma też APS/ZDO, więc sprawdź, czy pełny stos nie przejmuje callbacku NLDE-DATA.indication
 * (w razie potrzeby ogranicz stos do samej warstwy NWK, nazwa opcji zależy od wydania).
 *
 * Użycie (z katalogu scratch/ drzewa ns-3):
 *   ./ns3 run "zigbee-profile-gen --topology=topology.csv --out=wynik.csv"
 * lub z kołem pip `ns3`: patrz build.sh.
 *
 * Zakres modelu (ważne przy cytowaniu wyników): ns-3 Zigbee nie ma formalnej walidacji,
 * nie obsługuje m.in. zabezpieczeń, routingu źródłowego, utrzymania tras i potwierdzeń APS.
 */

#include "ns3/constant-position-mobility-model.h"
#include "ns3/core-module.h"
#include "ns3/lr-wpan-module.h"
#include "ns3/mobility-module.h"
#include "ns3/packet.h"
#include "ns3/propagation-module.h"
#include "ns3/single-model-spectrum-channel.h"
#include "ns3/zigbee-module.h"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <queue>
#include <sstream>
#include <string>
#include <vector>

using namespace ns3;
using namespace ns3::lrwpan;
using namespace ns3::zigbee;

NS_LOG_COMPONENT_DEFINE("ZigbeeProfileGen");

/** Znacznik pakietu z numerem sondy: pozwala jednoznacznie dopasować odbiór do wysyłki. */
class ProbeTag : public Tag
{
  public:
    ProbeTag() = default;

    explicit ProbeTag(uint32_t id)
        : m_id(id)
    {
    }

    static TypeId GetTypeId()
    {
        static TypeId tid =
            TypeId("ns3::ZigbeeProfileGenProbeTag").SetParent<Tag>().AddConstructor<ProbeTag>();
        return tid;
    }

    TypeId GetInstanceTypeId() const override
    {
        return GetTypeId();
    }

    uint32_t GetSerializedSize() const override
    {
        return 4;
    }

    void Serialize(TagBuffer i) const override
    {
        i.WriteU32(m_id);
    }

    void Deserialize(TagBuffer i) override
    {
        m_id = i.ReadU32();
    }

    void Print(std::ostream& os) const override
    {
        os << "probe=" << m_id;
    }

    uint32_t m_id{0};
};

NS_OBJECT_ENSURE_REGISTERED(ProbeTag);

namespace
{

struct NodeSpec
{
    std::string name;
    bool coordinator{false};
    bool endDevice{false}; //!< urządzenie końcowe: nie przekazuje ruchu, nie uczestniczy w wykrywaniu tras
    bool probe{true};
};

struct LinkSpec
{
    std::string a;
    std::string b;
    double lossDb{0};
};

struct Probe
{
    uint32_t dev{0};  //!< indeks węzła mierzonego (nie koordynatora)
    bool up{true};    //!< true: urządzenie -> koordynator, false: koordynator -> urządzenie
    uint32_t seq{0};  //!< numer rundy
    bool warmup{false};
    double txTime{-1};
    double rxTime{-1};
};

struct Options
{
    std::string topology{"topology.csv"};
    std::string out{"ns3_results.csv"};
    std::string mode{"isolated"}; //!< isolated: jedna sonda naraz; loaded: równoległy ruch Poissona
    uint32_t probes{30};          //!< liczba mierzonych sond na urządzenie i kierunek (na jeden przebieg)
    uint32_t warmup{1};           //!< sondy rozgrzewkowe (wykrywanie tras), osobno w wynikach
    double slot{0.25};            //!< odstęp między sondami w trybie isolated [s]
    double interval{2.0};         //!< średni odstęp sond jednego strumienia w trybie loaded [s]
    uint32_t payload{20};         //!< rozmiar danych sondy [B]
    double timeout{5.0};          //!< po tym czasie spóźniona sonda liczy się jako utracona [s]
    double maxWindow{110.0};      //!< maks. czas od pierwszej sondy do ostatniej [s]; 0 = bez limitu
    double fading{1.5};           //!< parametr m zaniku Nakagami (0 = brak; 1 = Rayleigh; 1,5 = domyślne m0 ns-3)
    double joinGap{3.0};          //!< przerwa po dołączeniu węzła przed dołączaniem następnego [s]
    double settle{5.0};           //!< przerwa po dołączeniu ostatniego węzła przed sondami [s]
    uint32_t seed{1};
    uint32_t run{1};
    bool verbose{false};
    bool dump{false};             //!< wypisz tablice sąsiadów, tras i wykrywania tras po rozgrzewce
    double dumpAt{-1};            //!< dodatkowy zrzut tablic w podanej chwili symulacji [s] (diagnostyka)
};

struct Scenario
{
    Options opt;
    std::vector<NodeSpec> nodes;
    std::vector<LinkSpec> links;
    std::vector<uint32_t> hops;      //!< hopy od koordynatora (BFS po grafie łączy)
    std::vector<uint32_t> joinOrder; //!< kolejność dołączania (BFS, bez koordynatora)
    std::vector<uint32_t> probeDevs; //!< węzły mierzone
    uint32_t coordIdx{0};

    NetDeviceContainer devs;
    ZigbeeStackContainer stacks;
    std::vector<Mac16Address> shortAddr;
    std::vector<uint32_t> joinAttempts;
    std::vector<double> joinTime;

    std::vector<Probe> probes;
    uint32_t unmatched{0};
    uint32_t misdelivered{0};
    uint32_t duplicates{0};
    std::map<int, uint32_t> dataConfirmStatus;

    bool joinFailed{false};
    bool joinDone{false};

    Ptr<MatrixPropagationLossModel> matrix;
    Ptr<NakagamiPropagationLossModel> nakagami; //!< włączany dopiero po zakończeniu dołączania
};

Scenario g;
const uint32_t kMaxJoinAttempts = 6;

std::string
Trim(const std::string& s)
{
    const auto b = s.find_first_not_of(" \t\r\n");
    if (b == std::string::npos)
    {
        return "";
    }
    const auto e = s.find_last_not_of(" \t\r\n");
    return s.substr(b, e - b + 1);
}

std::vector<std::string>
Split(const std::string& s, char sep)
{
    std::vector<std::string> out;
    std::stringstream ss(s);
    std::string item;
    while (std::getline(ss, item, sep))
    {
        out.push_back(Trim(item));
    }
    return out;
}

int
FindNode(const std::string& name)
{
    for (size_t i = 0; i < g.nodes.size(); ++i)
    {
        if (g.nodes[i].name == name)
        {
            return static_cast<int>(i);
        }
    }
    return -1;
}

bool
LoadTopology(const std::string& path, std::string& err)
{
    std::ifstream in(path);
    if (!in)
    {
        err = "nie mogę otworzyć pliku topologii: " + path;
        return false;
    }
    std::string line;
    int lineNo = 0;
    while (std::getline(in, line))
    {
        ++lineNo;
        const auto hash = line.find('#');
        if (hash != std::string::npos)
        {
            line = line.substr(0, hash);
        }
        line = Trim(line);
        if (line.empty())
        {
            continue;
        }
        const auto f = Split(line, ',');
        std::ostringstream where;
        where << path << ":" << lineNo << ": ";
        if (f[0] == "node")
        {
            if (f.size() < 3 || f.size() > 4)
            {
                err = where.str() + "oczekiwano: node,<nazwa>,<coordinator|router|enddevice>[,<probe>]";
                return false;
            }
            if (FindNode(f[1]) >= 0)
            {
                err = where.str() + "zduplikowana nazwa węzła: " + f[1];
                return false;
            }
            NodeSpec n;
            n.name = f[1];
            if (f[2] == "coordinator")
            {
                n.coordinator = true;
                n.probe = false;
            }
            else if (f[2] == "enddevice")
            {
                n.endDevice = true;
            }
            else if (f[2] != "router")
            {
                err = where.str() + "rola musi być coordinator, router albo enddevice, jest: " +
                      f[2];
                return false;
            }
            if (f.size() == 4 && !n.coordinator)
            {
                n.probe = (f[3] != "0");
            }
            g.nodes.push_back(n);
        }
        else if (f[0] == "link")
        {
            if (f.size() != 4)
            {
                err = where.str() + "oczekiwano: link,<A>,<B>,<tłumienie dB>";
                return false;
            }
            LinkSpec l;
            l.a = f[1];
            l.b = f[2];
            char* endp = nullptr;
            l.lossDb = std::strtod(f[3].c_str(), &endp);
            if (endp == f[3].c_str() || *endp != '\0' || l.lossDb <= 0)
            {
                err = where.str() + "tłumienie musi być liczbą dodatnią: " + f[3];
                return false;
            }
            if (FindNode(l.a) < 0 || FindNode(l.b) < 0)
            {
                err = where.str() + "link do nieznanego węzła (węzły muszą być zdefiniowane wcześniej)";
                return false;
            }
            if (g.nodes[FindNode(l.a)].endDevice && g.nodes[FindNode(l.b)].endDevice)
            {
                err = where.str() + "link między dwoma urządzeniami końcowymi (" + l.a + ", " + l.b +
                      ") jest niedozwolony: urządzenie końcowe łączy się tylko z routerem/koordynatorem";
                return false;
            }
            g.links.push_back(l);
        }
        else
        {
            err = where.str() + "nieznany typ wiersza: " + f[0];
            return false;
        }
    }

    int coordinators = 0;
    for (size_t i = 0; i < g.nodes.size(); ++i)
    {
        if (g.nodes[i].coordinator)
        {
            ++coordinators;
            g.coordIdx = static_cast<uint32_t>(i);
        }
    }
    if (coordinators != 1)
    {
        err = "topologia musi mieć dokładnie jednego koordynatora (jest " +
              std::to_string(coordinators) + ")";
        return false;
    }

    // BFS od koordynatora: hopy i kolejność dołączania
    const size_t n = g.nodes.size();
    std::vector<std::vector<uint32_t>> adj(n);
    for (const auto& l : g.links)
    {
        const uint32_t a = FindNode(l.a);
        const uint32_t b = FindNode(l.b);
        adj[a].push_back(b);
        adj[b].push_back(a);
    }
    g.hops.assign(n, UINT32_MAX);
    g.hops[g.coordIdx] = 0;
    std::queue<uint32_t> q;
    q.push(g.coordIdx);
    while (!q.empty())
    {
        const uint32_t u = q.front();
        q.pop();
        if (g.nodes[u].endDevice)
        {
            continue; // urządzenie końcowe nie przekazuje ruchu, więc nie wydłuża ścieżek
        }
        for (uint32_t v : adj[u])
        {
            if (g.hops[v] == UINT32_MAX)
            {
                g.hops[v] = g.hops[u] + 1;
                g.joinOrder.push_back(v);
                q.push(v);
            }
        }
    }
    for (size_t i = 0; i < n; ++i)
    {
        if (g.hops[i] == UINT32_MAX)
        {
            err = "węzeł " + g.nodes[i].name + " jest nieosiągalny z koordynatora";
            return false;
        }
        if (g.nodes[i].probe && !g.nodes[i].coordinator)
        {
            g.probeDevs.push_back(static_cast<uint32_t>(i));
        }
    }
    if (g.probeDevs.empty())
    {
        err = "brak węzłów do pomiaru (probe=1)";
        return false;
    }
    return true;
}

// ----------------------------------------------------------------------------- dołączanie

void StartJoin(uint32_t pos);

void
Fail(const std::string& msg)
{
    std::cerr << "BŁĄD: " << msg << std::endl;
    g.joinFailed = true;
    Simulator::Stop();
}

void
RetryJoin(uint32_t pos, const std::string& why)
{
    const uint32_t idx = g.joinOrder[pos];
    if (g.joinAttempts[idx] >= kMaxJoinAttempts)
    {
        Fail("węzeł " + g.nodes[idx].name + " nie dołączył po " +
             std::to_string(kMaxJoinAttempts) + " próbach (" + why + ")");
        return;
    }
    if (g.opt.verbose)
    {
        std::cout << "[t=" << Simulator::Now().GetSeconds() << "s] " << g.nodes[idx].name
                  << ": ponawiam dołączanie (" << why << ")" << std::endl;
    }
    Simulator::Schedule(Seconds(2.0), &StartJoin, pos);
}

uint32_t g_joinPos = 0; //!< pozycja w joinOrder aktualnie dołączanego węzła

void
StartProbes();

void
StartJoin(uint32_t pos)
{
    if (g.joinFailed)
    {
        return;
    }
    if (pos >= g.joinOrder.size())
    {
        g.joinDone = true;
        std::cout << "[t=" << Simulator::Now().GetSeconds() << "s] wszystkie węzły dołączyły ("
                  << g.joinOrder.size() << "), start sond za " << g.opt.settle << " s" << std::endl;
        Simulator::Schedule(Seconds(g.opt.settle), &StartProbes);
        return;
    }
    g_joinPos = pos;
    const uint32_t idx = g.joinOrder[pos];
    g.joinAttempts[idx]++;

    NlmeNetworkDiscoveryRequestParams p;
    p.m_scanChannelList.channelPageCount = 1;
    p.m_scanChannelList.channelsField[0] = ALL_CHANNELS;
    p.m_scanDuration = 2;
    Ptr<ZigbeeStack> st = g.stacks.Get(idx);
    Simulator::ScheduleWithContext(st->GetNode()->GetId(),
                                   Seconds(0),
                                   &ZigbeeNwk::NlmeNetworkDiscoveryRequest,
                                   st->GetNwk(),
                                   p);
}

void
OnFormationConfirm(NlmeNetworkFormationConfirmParams params)
{
    if (params.m_status != NwkStatus::SUCCESS)
    {
        Fail("koordynator nie utworzył sieci, status NWK=" +
             std::to_string(static_cast<int>(params.m_status)));
        return;
    }
    std::cout << "[t=" << Simulator::Now().GetSeconds() << "s] koordynator utworzył sieć"
              << std::endl;
    Simulator::Schedule(Seconds(1.0), &StartJoin, 0);
}

void
OnDiscoveryConfirm(uint32_t idx, NlmeNetworkDiscoveryConfirmParams params)
{
    if (g.joinFailed || g.joinOrder[g_joinPos] != idx)
    {
        return;
    }
    if (params.m_status != NwkStatus::SUCCESS || params.m_netDescList.empty())
    {
        RetryJoin(g_joinPos, "brak sieci w zasięgu, status=" +
                                 std::to_string(static_cast<int>(params.m_status)));
        return;
    }
    NlmeJoinRequestParams j;
    CapabilityInformation capability;
    capability.SetDeviceType(g.nodes[idx].endDevice ? zigbee::ENDDEVICE : zigbee::ROUTER);
    capability.SetAllocateAddrOn(true);
    j.m_rejoinNetwork = ASSOCIATION;
    j.m_capabilityInfo = capability.GetCapability();
    j.m_extendedPanId = params.m_netDescList[0].m_extPanId;
    Ptr<ZigbeeStack> st = g.stacks.Get(idx);
    Simulator::ScheduleNow(&ZigbeeNwk::NlmeJoinRequest, st->GetNwk(), j);
}

void
OnJoinConfirm(uint32_t idx, NlmeJoinConfirmParams params)
{
    if (g.joinFailed || g.joinOrder[g_joinPos] != idx)
    {
        return;
    }
    if (params.m_status != NwkStatus::SUCCESS)
    {
        RetryJoin(g_joinPos,
                  "asocjacja nieudana, status=" +
                      std::to_string(static_cast<int>(params.m_status)));
        return;
    }
    g.shortAddr[idx] = params.m_networkAddress;
    g.joinTime[idx] = Simulator::Now().GetSeconds();
    std::cout << "[t=" << Simulator::Now().GetSeconds() << "s] " << g.nodes[idx].name
              << " dołączył (hopy=" << g.hops[idx] << ", adres krótki=" << params.m_networkAddress
              << ", próba " << g.joinAttempts[idx] << ")" << std::endl;

    if (!g.nodes[idx].endDevice)
    {
        NlmeStartRouterRequestParams r; // domyślne wartości, jak w przykładach ns-3
        Ptr<ZigbeeStack> st = g.stacks.Get(idx);
        Simulator::ScheduleNow(&ZigbeeNwk::NlmeStartRouterRequest, st->GetNwk(), r);
    }
    Simulator::Schedule(Seconds(g.opt.joinGap), &StartJoin, g_joinPos + 1);
}

// ----------------------------------------------------------------------------- sondy

void
OnDataIndication(uint32_t idx, NldeDataIndicationParams params, Ptr<Packet> p)
{
    ProbeTag tag;
    if (!p->PeekPacketTag(tag) || tag.m_id >= g.probes.size())
    {
        g.unmatched++;
        return;
    }
    Probe& pr = g.probes[tag.m_id];
    const uint32_t expected = pr.up ? g.coordIdx : pr.dev;
    if (idx != expected)
    {
        g.misdelivered++;
        return;
    }
    if (pr.rxTime >= 0)
    {
        g.duplicates++;
        return;
    }
    pr.rxTime = Simulator::Now().GetSeconds();
}

void
OnDataConfirm(uint32_t idx, NldeDataConfirmParams params)
{
    g.dataConfirmStatus[static_cast<int>(params.m_status)]++;
}

void
SendProbe(uint32_t probeId)
{
    Probe& pr = g.probes[probeId];
    pr.txTime = Simulator::Now().GetSeconds();

    Ptr<Packet> p = Create<Packet>(g.opt.payload);
    p->AddPacketTag(ProbeTag(probeId));

    const uint32_t src = pr.up ? pr.dev : g.coordIdx;
    const uint32_t dst = pr.up ? g.coordIdx : pr.dev;

    NldeDataRequestParams req;
    req.m_dstAddrMode = UCST_BCST;
    req.m_dstAddr = g.stacks.Get(dst)->GetNwk()->GetNetworkAddress();
    req.m_nsduHandle = static_cast<uint8_t>(probeId & 0xFF);
    req.m_nsduLength = p->GetSize();
    req.m_discoverRoute = ENABLE_ROUTE_DISCOVERY;

    Ptr<ZigbeeStack> st = g.stacks.Get(src);
    Simulator::ScheduleWithContext(st->GetNode()->GetId(),
                                   Seconds(0),
                                   &ZigbeeNwk::NldeDataRequest,
                                   st->GetNwk(),
                                   req,
                                   p);
}

uint32_t
AddProbe(uint32_t dev, bool up, uint32_t seq, bool warmup, double at)
{
    Probe pr;
    pr.dev = dev;
    pr.up = up;
    pr.seq = seq;
    pr.warmup = warmup;
    g.probes.push_back(pr);
    const uint32_t id = static_cast<uint32_t>(g.probes.size() - 1);
    Simulator::Schedule(Seconds(at), &SendProbe, id);
    return id;
}

void
DumpTables()
{
    Ptr<OutputStreamWrapper> out = Create<OutputStreamWrapper>(&std::cout);
    for (uint32_t i = 0; i < g.nodes.size(); ++i)
    {
        Ptr<ZigbeeNwk> nwk = g.stacks.Get(i)->GetNwk();
        std::cout << "\n===== " << g.nodes[i].name << " (adres " << nwk->GetNetworkAddress()
                  << ") =====\n--- tablica sąsiadów\n";
        nwk->PrintNeighborTable(out);
        std::cout << "--- tablica tras\n";
        nwk->PrintRoutingTable(out);
        std::cout << "--- tablica wykrywania tras\n";
        nwk->PrintRouteDiscoveryTable(out);
    }
    std::cout << std::endl;
}

void
StartProbes()
{
    const Options& o = g.opt;
    double t = 0;
    const double warmSlot = std::max(o.slot, 1.5); // wykrywanie trasy może trwać dłużej niż slot

    if (g.nakagami)
    {
        g.matrix->SetNext(g.nakagami); // od tej chwili każda ramka ma losowy zanik
    }

    // Zabezpieczenie: w ns-3 3.44 wpis trasy w routerze wygasa ok. 128 s po utworzeniu i nie jest
    // odnawiany użyciem. Po wygaśnięciu KAŻDY przekazywany pakiet wymaga nowego wykrywania trasy
    // (RREQ z losowym opóźnieniem), co zawyża opóźnienia na ścieżkach przez routery. Dlatego okno
    // pomiarowe (od pierwszej sondy rozgrzewkowej do ostatniej mierzonej) musi być krótsze.
    {
        uint32_t flows = 0;
        for (uint32_t dev : g.probeDevs)
        {
            flows += g.nodes[dev].endDevice ? 1 : 2; // urządzenie końcowe: tylko w górę
        }
        const double warmDur = o.warmup * flows * warmSlot;
        double window = warmDur;
        if (o.mode == "isolated")
        {
            window += o.probes * flows * o.slot;
        }
        else
        {
            window += 2.0 + (o.probes + 3.0 * std::sqrt(static_cast<double>(o.probes)) + 1.0) *
                                o.interval; // średnia + 3 sigma sumy wykładniczych odstępów
        }
        if (o.maxWindow > 0 && window > o.maxWindow)
        {
            std::ostringstream m;
            m << "okno pomiarowe ~" << std::fixed << std::setprecision(0) << window
              << " s przekracza --maxWindow=" << o.maxWindow
              << " s (w ns-3 3.44 trasy wygasają po ~128 s i wyniki byłyby zawyżone). "
                 "Zmniejsz --probes lub --slot/--interval i zbierz więcej przebiegów (--run=1..N, "
                 "patrz run_batch.sh). Limit można wyłączyć przez --maxWindow=0.";
            Fail(m.str());
            return;
        }
    }

    // faza 1: rozgrzewka, sekwencyjnie (pierwsza sonda każdego kierunku wykrywa trasę)
    for (uint32_t r = 0; r < o.warmup; ++r)
    {
        for (bool up : {true, false})
        {
            for (uint32_t dev : g.probeDevs)
            {
                if (!up && g.nodes[dev].endDevice)
                {
                    continue;
                }
                AddProbe(dev, up, r, true, t);
                t += warmSlot;
            }
        }
    }

    if (o.dump)
    {
        Simulator::Schedule(Seconds(std::max(0.0, t - 0.1)), &DumpTables);
    }
    if (o.dumpAt >= 0 && o.dumpAt > Simulator::Now().GetSeconds())
    {
        Simulator::Schedule(Seconds(o.dumpAt) - Simulator::Now(), &DumpTables);
    }

    // faza 2: pomiar
    if (o.mode == "isolated")
    {
        for (uint32_t r = 0; r < o.probes; ++r)
        {
            for (bool up : {true, false})
            {
                for (uint32_t dev : g.probeDevs)
                {
                    if (!up && g.nodes[dev].endDevice)
                    {
                        continue;
                    }
                    AddProbe(dev, up, o.warmup + r, false, t);
                    t += o.slot;
                }
            }
        }
    }
    else
    {
        Ptr<ExponentialRandomVariable> gap = CreateObject<ExponentialRandomVariable>();
        gap->SetAttribute("Mean", DoubleValue(o.interval));
        gap->SetStream(7000);
        Ptr<UniformRandomVariable> offset = CreateObject<UniformRandomVariable>();
        offset->SetAttribute("Max", DoubleValue(o.interval));
        offset->SetStream(7001);
        const double start = t + 2.0;
        double end = start;
        for (bool up : {true, false})
        {
            for (uint32_t dev : g.probeDevs)
            {
                if (!up && g.nodes[dev].endDevice)
                {
                    continue;
                }
                double at = start + offset->GetValue();
                for (uint32_t r = 0; r < o.probes; ++r)
                {
                    AddProbe(dev, up, o.warmup + r, false, at);
                    end = std::max(end, at);
                    at += gap->GetValue();
                }
            }
        }
        t = end;
    }
    std::cout << "[t=" << Simulator::Now().GetSeconds() << "s] zaplanowano " << g.probes.size()
              << " sond (tryb " << o.mode << "), koniec symulacji za " << t + o.timeout + 2.0
              << " s" << std::endl;
    Simulator::Stop(Seconds(t + o.timeout + 2.0));
}

// ----------------------------------------------------------------------------- wynik

void
WriteResults()
{
    std::ofstream out(g.opt.out);
    if (!out)
    {
        std::cerr << "BŁĄD: nie mogę zapisać " << g.opt.out << std::endl;
        std::exit(1);
    }
    const Options& o = g.opt;
    out << "# zigbee-profile-gen fading=" << o.fading << " mode=" << o.mode << " probes=" << o.probes
        << " warmup=" << o.warmup << " slot=" << o.slot << " interval=" << o.interval
        << " payload=" << o.payload << " timeout=" << o.timeout << " seed=" << o.seed
        << " run=" << o.run << "\n";
    out << "mode,run,direction,device,hops,seq,warmup,tx_s,rx_s,latency_ms,status\n";
    out << std::fixed;
    for (const Probe& pr : g.probes)
    {
        const bool got = pr.rxTime >= 0;
        const double lat = got ? (pr.rxTime - pr.txTime) * 1000.0 : 0;
        const char* status = !got ? "lost" : (lat > o.timeout * 1000.0 ? "late" : "ok");
        out << o.mode << "," << o.run << "," << (pr.up ? "up" : "down") << ","
            << g.nodes[pr.dev].name << "," << g.hops[pr.dev] << "," << pr.seq << ","
            << (pr.warmup ? 1 : 0) << "," << std::setprecision(6) << pr.txTime << ",";
        if (got)
        {
            out << std::setprecision(6) << pr.rxTime << "," << std::setprecision(3) << lat;
        }
        else
        {
            out << ",";
        }
        out << "," << status << "\n";
    }
}

void
PrintSummary()
{
    std::cout << "\nPodsumowanie (bez sond rozgrzewkowych; opóźnienie tylko z sond ok):\n";
    std::cout << std::left << std::setw(24) << "urządzenie" << std::right << std::setw(5) << "hopy"
              << std::setw(7) << "kier." << std::setw(8) << "sond" << std::setw(11) << "utrata %"
              << std::setw(11) << "śr. [ms]" << std::setw(11) << "maks [ms]" << "\n";
    for (uint32_t dev : g.probeDevs)
    {
        for (bool up : {true, false})
        {
            if (!up && g.nodes[dev].endDevice)
            {
                continue;
            }
            uint32_t n = 0;
            uint32_t ok = 0;
            double sum = 0;
            double mx = 0;
            for (const Probe& pr : g.probes)
            {
                if (pr.dev != dev || pr.up != up || pr.warmup)
                {
                    continue;
                }
                ++n;
                if (pr.rxTime >= 0 && (pr.rxTime - pr.txTime) <= g.opt.timeout)
                {
                    ++ok;
                    const double l = (pr.rxTime - pr.txTime) * 1000.0;
                    sum += l;
                    mx = std::max(mx, l);
                }
            }
            std::cout << std::left << std::setw(24) << g.nodes[dev].name << std::right
                      << std::setw(5) << g.hops[dev] << std::setw(7) << (up ? "up" : "down")
                      << std::setw(8) << n << std::fixed << std::setprecision(1) << std::setw(11)
                      << (n ? 100.0 * (n - ok) / n : 0.0) << std::setw(11)
                      << (ok ? sum / ok : 0.0) << std::setw(11) << mx << "\n";
        }
    }
    // Status NLDE-DATA.confirm w ns-3 3.44 jest zawsze domyślnym 0xC1 (INVALID_PARAMETER), więc nie
    // niesie informacji; o powodzeniu decyduje wyłącznie dotarcie sondy (NLDE-DATA.indication).
    std::cout << "\nOdbiory bez znacznika: " << g.unmatched << ", dostarczone do złego węzła: "
              << g.misdelivered << ", duplikaty: " << g.duplicates << std::endl;
}

} // namespace

int
main(int argc, char* argv[])
{
    Options& o = g.opt;
    CommandLine cmd(__FILE__);
    cmd.AddValue("topology", "plik topologii CSV", o.topology);
    cmd.AddValue("out", "plik wynikowy CSV (jeden wiersz na sondę)", o.out);
    cmd.AddValue("mode", "isolated (jedna sonda naraz) albo loaded (równoległy ruch Poissona)", o.mode);
    cmd.AddValue("probes", "mierzone sondy na urządzenie i kierunek", o.probes);
    cmd.AddValue("warmup", "sondy rozgrzewkowe na urządzenie i kierunek", o.warmup);
    cmd.AddValue("slot", "odstęp sond w trybie isolated [s]", o.slot);
    cmd.AddValue("interval", "średni odstęp sond strumienia w trybie loaded [s]", o.interval);
    cmd.AddValue("payload", "rozmiar danych sondy [B]", o.payload);
    cmd.AddValue("timeout", "limit czasu; późniejsza sonda liczy się jako utracona [s]", o.timeout);
    cmd.AddValue("fading",
                 "szybki zanik Nakagami-m na każdym łączu (0 = wyłączony, 1 = Rayleigh, 1.5 = "
                 "domyślne m0 w ns-3, większe m = łagodniejszy zanik); losuje moc odbioru osobno dla "
                 "każdej ramki. Bez zaniku łącza są prawie binarne (patrz calibrate_link.sh)",
                 o.fading);
    cmd.AddValue("maxWindow",
                 "maks. okno pomiarowe od pierwszej do ostatniej sondy [s] (0 = bez limitu); w ns-3 3.44 "
                 "trasy wygasają ok. 128 s po utworzeniu, co zafałszowałoby opóźnienia",
                 o.maxWindow);
    cmd.AddValue("joinGap", "przerwa między dołączaniem węzłów [s]", o.joinGap);
    cmd.AddValue("settle", "przerwa po ostatnim dołączeniu przed sondami [s]", o.settle);
    cmd.AddValue("seed", "ziarno generatora losowego", o.seed);
    cmd.AddValue("run", "numer przebiegu (ten sam seed, inny przebieg)", o.run);
    cmd.AddValue("verbose", "więcej komunikatów", o.verbose);
    cmd.AddValue("dump", "wypisz tablice sąsiadów i tras po rozgrzewce (diagnostyka)", o.dump);
    cmd.AddValue("dumpAt", "dodatkowy zrzut tablic w chwili t [s] (diagnostyka, <0 = wyłączony)", o.dumpAt);
    cmd.Parse(argc, argv);

    if (o.mode != "isolated" && o.mode != "loaded")
    {
        std::cerr << "BŁĄD: --mode musi być isolated albo loaded" << std::endl;
        return 1;
    }
    if (o.payload == 0 || o.payload > 80)
    {
        std::cerr << "BŁĄD: --payload musi być w zakresie 1-80 B (ramka 802.15.4 ma 127 B)"
                  << std::endl;
        return 1;
    }
    std::string err;
    if (!LoadTopology(o.topology, err))
    {
        std::cerr << "BŁĄD topologii: " << err << std::endl;
        return 1;
    }

    RngSeedManager::SetSeed(o.seed);
    RngSeedManager::SetRun(o.run);

    const uint32_t n = static_cast<uint32_t>(g.nodes.size());
    NodeContainer nodes;
    nodes.Create(n);

    // Kanał: tłumienie zadane macierzą (poza wymienionymi parami tłumienie 200 dB = brak zasięgu)
    Ptr<SingleModelSpectrumChannel> channel = CreateObject<SingleModelSpectrumChannel>();
    Ptr<MatrixPropagationLossModel> matrix = CreateObject<MatrixPropagationLossModel>();
    matrix->SetDefaultLoss(200);
    g.matrix = matrix;
    if (o.fading > 0)
    {
        // Zanik działa na moc po tłumieniu z macierzy. Odległość węzłów jest zerowa (położenia
        // nieistotne), więc obowiązuje parametr m0; ustawiamy wszystkie trzy na tę samą wartość.
        // Podłączamy go dopiero w StartProbes(): dołączanie węzłów (skan, asocjacja) używa ramek
        // rozgłoszeniowych bez retransmisji i przy zaniku bywa niestabilne, a mierzyć chcemy ruch.
        g.nakagami = CreateObject<NakagamiPropagationLossModel>();
        g.nakagami->SetAttribute("m0", DoubleValue(o.fading));
        g.nakagami->SetAttribute("m1", DoubleValue(o.fading));
        g.nakagami->SetAttribute("m2", DoubleValue(o.fading));
        g.nakagami->AssignStreams(3000);
    }
    channel->AddPropagationLossModel(matrix);
    channel->SetPropagationDelayModel(CreateObject<ConstantSpeedPropagationDelayModel>());

    LrWpanHelper lrWpanHelper;
    lrWpanHelper.SetChannel(channel);
    g.devs = lrWpanHelper.Install(nodes);

    std::vector<Ptr<ConstantPositionMobilityModel>> mob(n);
    for (uint32_t i = 0; i < n; ++i)
    {
        Ptr<LrWpanNetDevice> dev = g.devs.Get(i)->GetObject<LrWpanNetDevice>();
        std::ostringstream addr;
        addr << "00:00:00:00:00:00:00:" << std::hex << std::setw(2) << std::setfill('0')
             << (i + 1);
        dev->GetMac()->SetExtendedAddress(Mac64Address(addr.str().c_str()));
        mob[i] = CreateObject<ConstantPositionMobilityModel>();
        mob[i]->SetPosition(Vector(0, 0, 0)); // położenie nieistotne, tłumienie zadaje macierz
        dev->GetPhy()->SetMobility(mob[i]);
    }
    for (const LinkSpec& l : g.links)
    {
        matrix->SetLoss(mob[FindNode(l.a)], mob[FindNode(l.b)], l.lossDb, true);
    }

    ZigbeeHelper zigbeeHelper;
    g.stacks = zigbeeHelper.Install(g.devs);
    // W ns-3 3.44 ZigbeeHelper::AssignStreams jest zadeklarowane, ale nie ma implementacji
    // (błąd konsolidacji), więc ustawiamy strumienie tylko dla lr-wpan i własnych zmiennych losowych.
    lrWpanHelper.AssignStreams(g.devs, 0);

    g.shortAddr.assign(n, Mac16Address("FF:FF"));
    g.joinAttempts.assign(n, 0);
    g.joinTime.assign(n, -1);

    for (uint32_t i = 0; i < n; ++i)
    {
        Ptr<ZigbeeNwk> nwk = g.stacks.Get(i)->GetNwk();
        nwk->SetNldeDataIndicationCallback(MakeBoundCallback(&OnDataIndication, i));
        nwk->SetNldeDataConfirmCallback(MakeBoundCallback(&OnDataConfirm, i));
        if (i == g.coordIdx)
        {
            nwk->SetNlmeNetworkFormationConfirmCallback(MakeCallback(&OnFormationConfirm));
        }
        else
        {
            nwk->SetNlmeNetworkDiscoveryConfirmCallback(MakeBoundCallback(&OnDiscoveryConfirm, i));
            nwk->SetNlmeJoinConfirmCallback(MakeBoundCallback(&OnJoinConfirm, i));
        }
    }

    std::cout << "Topologia: " << n << " węzłów, " << g.links.size() << " łączy, mierzonych "
              << g.probeDevs.size() << ", tryb " << o.mode << ", seed " << o.seed << "/" << o.run
              << "\n";
    for (uint32_t dev : g.probeDevs)
    {
        std::cout << "  " << std::left << std::setw(24) << g.nodes[dev].name << std::right
                  << " hopy=" << g.hops[dev] << "\n";
    }

    // koordynator formuje sieć
    {
        NlmeNetworkFormationRequestParams f;
        f.m_scanChannelList.channelPageCount = 1;
        f.m_scanChannelList.channelsField[0] = ALL_CHANNELS;
        f.m_scanDuration = 0;
        f.m_superFrameOrder = 15;
        f.m_beaconOrder = 15;
        Ptr<ZigbeeStack> st = g.stacks.Get(g.coordIdx);
        Simulator::ScheduleWithContext(st->GetNode()->GetId(),
                                       Seconds(1.0),
                                       &ZigbeeNwk::NlmeNetworkFormationRequest,
                                       st->GetNwk(),
                                       f);
    }
    // straż: dołączanie nie może trwać w nieskończoność
    const double joinDeadline =
        10.0 + g.joinOrder.size() * (kMaxJoinAttempts * 8.0 + o.joinGap + 5.0);
    Simulator::Schedule(Seconds(joinDeadline), []() {
        if (!g.joinDone && !g.joinFailed)
        {
            Fail("przekroczono limit czasu dołączania węzłów");
        }
    });

    Simulator::Run();
    Simulator::Destroy();

    if (g.joinFailed || !g.joinDone)
    {
        return 2;
    }
    WriteResults();
    PrintSummary();
    std::cout << "Wyniki zapisano do " << o.out << std::endl;
    return 0;
}
