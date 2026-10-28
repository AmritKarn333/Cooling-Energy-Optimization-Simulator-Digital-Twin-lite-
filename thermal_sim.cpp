// Primitive 2D thermal room simulator (NOT CFD).
// Usage: thermal_sim [config.cfg] [temperature.csv]
// Prints one JSON object with metrics on the last line of stdout.
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

struct Rack { double x, y, w, h, heat; };
struct Fan { double x, y, w, h; std::string dir; double strength; };
struct Cooling { double x, y, w, h; std::string mode; double strength, temp; bool has_temp; };

struct Config {
    double width = 100, height = 100, ambient = 24, cool_temp = 18;
    double duration = 120, dt = 0.05, diffusivity = 0.35;
    int nx = 80, ny = 80;
    std::vector<Rack> racks;
    std::vector<Fan> fans;
    std::vector<Cooling> coolings;
};

[[noreturn]] static void fail(int line, const std::string& msg) {
    throw std::runtime_error("config line " + std::to_string(line) + ": " + msg);
}

static std::string trim(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\r\n");
    return s.substr(a, b - a + 1);
}

static std::vector<std::string> split(const std::string& s, char delim = ',') {
    std::vector<std::string> v;
    std::stringstream ss(s);
    std::string item;
    while (std::getline(ss, item, delim)) v.push_back(trim(item));
    return v;
}

static double parseNum(const std::string& s, const std::string& what, int line) {
    size_t pos = 0;
    double v = 0;
    try { v = std::stod(s, &pos); }
    catch (const std::exception&) { fail(line, "'" + s + "' is not a number (" + what + ")"); }
    if (pos != s.size() || !std::isfinite(v)) fail(line, "'" + s + "' is not a valid number (" + what + ")");
    return v;
}

static int parseInt(const std::string& s, const std::string& what, int line) {
    double v = parseNum(s, what, line);
    if (v != std::floor(v)) fail(line, what + " must be a whole number");
    return (int)std::max(-1e6, std::min(1e6, v));
}

static bool oneOf(const std::string& s, std::initializer_list<const char*> opts) {
    for (const char* o : opts) if (s == o) return true;
    return false;
}

static Config loadConfig(const std::string& path) {
    Config c;
    std::ifstream f(path);
    if (!f) throw std::runtime_error("Cannot open config: " + path);
    std::string raw;
    int ln = 0;
    while (std::getline(f, raw)) {
        ++ln;
        auto hash = raw.find('#');
        if (hash != std::string::npos) raw.erase(hash);
        std::string line = trim(raw);
        if (line.empty()) continue;
        auto p = line.find('=');
        if (p == std::string::npos) fail(ln, "expected key=value");
        std::string key = trim(line.substr(0, p)), val = trim(line.substr(p + 1));
        auto num = [&](const std::string& s, const std::string& what) { return parseNum(s, what, ln); };

        if (key == "width") c.width = num(val, "width");
        else if (key == "height") c.height = num(val, "height");
        else if (key == "nx") c.nx = parseInt(val, "nx", ln);
        else if (key == "ny") c.ny = parseInt(val, "ny", ln);
        else if (key == "ambient_temp") c.ambient = num(val, "ambient_temp");
        else if (key == "cool_temp") c.cool_temp = num(val, "cool_temp");
        else if (key == "duration") c.duration = num(val, "duration");
        else if (key == "dt") c.dt = num(val, "dt");
        else if (key == "diffusivity") c.diffusivity = num(val, "diffusivity");
        else if (key == "rack") {
            auto a = split(val);
            if (a.size() < 5) fail(ln, "rack needs x,y,w,h,heat_rate");
            Rack r{num(a[0], "rack x"), num(a[1], "rack y"), num(a[2], "rack w"), num(a[3], "rack h"), num(a[4], "rack heat_rate")};
            if (r.w <= 0 || r.h <= 0) fail(ln, "rack width/height must be positive");
            if (r.heat < 0) fail(ln, "rack heat_rate cannot be negative");
            c.racks.push_back(r);
        } else if (key == "fan") {
            auto a = split(val);
            if (a.size() < 6) fail(ln, "fan needs x,y,w,h,direction,strength");
            Fan fn{num(a[0], "fan x"), num(a[1], "fan y"), num(a[2], "fan w"), num(a[3], "fan h"), a[4], num(a[5], "fan strength")};
            if (fn.w <= 0 || fn.h <= 0) fail(ln, "fan width/height must be positive");
            if (fn.strength < 0) fail(ln, "fan strength cannot be negative");
            if (!oneOf(fn.dir, {"up", "down", "left", "right"})) fail(ln, "fan direction must be up, down, left or right (got '" + fn.dir + "')");
            c.fans.push_back(fn);
        } else if (key == "cooling") {
            auto a = split(val);
            if (a.size() < 6) fail(ln, "cooling needs x,y,w,h,mode,strength[,target_temp]");
            Cooling q{num(a[0], "cooling x"), num(a[1], "cooling y"), num(a[2], "cooling w"), num(a[3], "cooling h"),
                      a[4], num(a[5], "cooling strength"), 0.0, false};
            if (a.size() >= 7 && !a[6].empty()) { q.temp = num(a[6], "cooling target_temp"); q.has_temp = true; }
            if (q.w <= 0 || q.h <= 0) fail(ln, "cooling width/height must be positive");
            if (q.strength < 0) fail(ln, "cooling strength cannot be negative");
            if (!oneOf(q.mode, {"fixed", "exhaust"})) fail(ln, "cooling mode must be fixed or exhaust (got '" + q.mode + "')");
            c.coolings.push_back(q);
        } else {
            std::cerr << "thermal_sim warning: line " << ln << ": unknown key '" << key << "' ignored\n";
        }
    }
    if (c.width <= 0 || c.height <= 0) throw std::runtime_error("width and height must be positive");
    if (c.dt <= 0) throw std::runtime_error("dt must be positive");
    if (c.diffusivity < 0) throw std::runtime_error("diffusivity cannot be negative");
    if (c.duration < 0) throw std::runtime_error("duration cannot be negative");
    c.nx = std::clamp(c.nx, 8, 400);
    c.ny = std::clamp(c.ny, 8, 400);
    for (auto& q : c.coolings) if (!q.has_temp) q.temp = c.cool_temp;  // cool_temp = default target
    return c;
}

static std::string jsonEscape(const std::string& s) {
    std::string o;
    for (unsigned char ch : s) {
        if (ch == '"') o += "\\\"";
        else if (ch == '\\') o += "\\\\";
        else if (ch < 0x20) { char b[8]; std::snprintf(b, sizeof b, "\\u%04x", ch); o += b; }
        else o += (char)ch;
    }
    return o;
}

int main(int argc, char** argv) {
    try {
        const std::string cfgPath = argc > 1 ? argv[1] : "config.cfg";
        const std::string outPath = argc > 2 ? argv[2] : "temperature.csv";
        Config c = loadConfig(cfgPath);

        const int nx = c.nx, ny = c.ny;
        const double dx = c.width / nx, dy = c.height / ny;
        const size_t n = (size_t)nx * ny;
        auto idx = [&](int x, int y) { return (size_t)y * nx + x; };

        // Cells whose centre lies in the rectangle (same rule for sources, sinks and metrics).
        // A rectangle smaller than one cell falls back to the cell holding its centre.
        auto cellsIn = [&](double rx, double ry, double rw, double rh) {
            std::vector<size_t> v;
            for (int y = 0; y < ny; y++) {
                double py = (y + .5) * dy;
                if (py < ry || py >= ry + rh) continue;
                for (int x = 0; x < nx; x++) {
                    double px = (x + .5) * dx;
                    if (px >= rx && px < rx + rw) v.push_back(idx(x, y));
                }
            }
            if (v.empty()) {
                double cx = rx + rw / 2, cy = ry + rh / 2;
                if (cx >= 0 && cx < c.width && cy >= 0 && cy < c.height) v.push_back(idx((int)(cx / dx), (int)(cy / dy)));
            }
            return v;
        };

        // ---- static fields: heat sources, cooling sinks, fan velocity ----
        std::vector<double> src(n, 0.0), sinkRate(n, 0.0), U(n, 0.0), V(n, 0.0);
        std::vector<char> isRack(n, 0);
        for (const auto& r : c.racks)
            for (size_t i : cellsIn(r.x, r.y, r.w, r.h)) { src[i] += r.heat; isRack[i] = 1; }

        struct Sink { double strength, target; std::vector<size_t> cells; };
        std::vector<Sink> sinks;
        for (const auto& q : c.coolings) {
            Sink s{q.strength, q.mode == "exhaust" ? c.ambient : q.temp, cellsIn(q.x, q.y, q.w, q.h)};
            for (size_t i : s.cells) sinkRate[i] += q.strength;
            sinks.push_back(std::move(s));
        }

        // A fan blows along a corridor downstream of its rectangle. The corridor half-width is 0.8x the
        // fan's cross-section (width for up/down fans, height for left/right fans), and strength fades
        // linearly across the corridor and exponentially along it.
        for (int y = 0; y < ny; y++) for (int x = 0; x < nx; x++) {
            double px = (x + .5) * dx, py = (y + .5) * dy, u = 0, v = 0;
            for (const auto& f : c.fans) {
                double fx = f.x + f.w / 2, fy = f.y + f.h / 2;
                if (f.dir == "down" || f.dir == "up") {
                    double half = std::max(f.w * 0.8, dx);
                    double lat = std::abs(px - fx);
                    if (lat > half) continue;
                    double fall = 1 - lat / half;
                    if (f.dir == "down" && py >= f.y + f.h) v += f.strength * fall * std::exp(-2 * (py - (f.y + f.h)) / std::max(c.height, 1.0));
                    if (f.dir == "up" && py <= f.y) v -= f.strength * fall * std::exp(-2 * (f.y - py) / std::max(c.height, 1.0));
                } else {
                    double half = std::max(f.h * 0.8, dy);
                    double lat = std::abs(py - fy);
                    if (lat > half) continue;
                    double fall = 1 - lat / half;
                    if (f.dir == "right" && px >= f.x + f.w) u += f.strength * fall * std::exp(-2 * (px - (f.x + f.w)) / std::max(c.width, 1.0));
                    if (f.dir == "left" && px <= f.x) u -= f.strength * fall * std::exp(-2 * (f.x - px) / std::max(c.width, 1.0));
                }
            }
            U[idx(x, y)] = u; V[idx(x, y)] = v;
        }

        // ---- time step: explicit scheme must be stable for diffusion + advection + sinks together ----
        const double AMB_K = 0.003;  // weak exchange with ambient
        double maxAdv = 0, maxSink = 0;
        for (size_t i = 0; i < n; i++) {
            maxAdv = std::max(maxAdv, std::abs(U[i]) / dx + std::abs(V[i]) / dy);
            maxSink = std::max(maxSink, sinkRate[i]);
        }
        const double rate = 2 * c.diffusivity * (1 / (dx * dx) + 1 / (dy * dy)) + maxAdv + maxSink + AMB_K;
        const double dtRequested = c.dt;
        const double dtMax = 0.8 / rate;
        const double dtCap = std::min(c.dt, dtMax);
        const int steps = c.duration > 0 ? std::max(1, (int)std::ceil(c.duration / dtCap)) : 0;
        const double dt = steps ? c.duration / steps : dtCap;  // steps*dt == duration exactly

        // ---- time loop ----
        std::vector<double> T(n, c.ambient), N(n, c.ambient);
        for (int step = 0; step < steps; step++) {
            for (int y = 0; y < ny; y++) for (int x = 0; x < nx; x++) {
                const size_t i = idx(x, y);
                const double t = T[i];
                const double tl = T[idx(std::max(x - 1, 0), y)], tr = T[idx(std::min(x + 1, nx - 1), y)];
                const double tu = T[idx(x, std::max(y - 1, 0))], td = T[idx(x, std::min(y + 1, ny - 1))];
                const double lap = (tl - 2 * t + tr) / (dx * dx) + (tu - 2 * t + td) / (dy * dy);
                const double dTdx = U[i] >= 0 ? (t - tl) / dx : (tr - t) / dx;  // first-order upwind
                const double dTdy = V[i] >= 0 ? (t - tu) / dy : (td - t) / dy;
                N[i] = t + dt * (c.diffusivity * lap - U[i] * dTdx - V[i] * dTdy + src[i] + AMB_K * (c.ambient - t));
            }
            for (const auto& s : sinks)
                for (size_t i : s.cells) N[i] += dt * s.strength * (s.target - T[i]);
            T.swap(N);
        }

        double minT = std::numeric_limits<double>::infinity(), maxT = -minT;
        for (double v : T) {
            if (!std::isfinite(v)) throw std::runtime_error("numerical instability: non-finite temperature (check dt, fan and cooling strengths)");
            minT = std::min(minT, v); maxT = std::max(maxT, v);
        }

        // ---- output ----
        std::ofstream csv(outPath);
        if (!csv) throw std::runtime_error("Cannot write output: " + outPath);
        csv << "# nx=" << nx << ",ny=" << ny << ",ambient=" << c.ambient << ",width=" << c.width << ",height=" << c.height << "\n";
        csv << std::setprecision(6);
        for (int y = 0; y < ny; y++) {
            for (int x = 0; x < nx; x++) { if (x) csv << ','; csv << T[idx(x, y)]; }
            csv << '\n';
        }
        csv.close();

        double sum = 0, rackMax = -std::numeric_limits<double>::infinity();
        long count = 0;
        for (size_t i = 0; i < n; i++) if (isRack[i]) { sum += T[i]; rackMax = std::max(rackMax, T[i]); count++; }
        const double avg = count ? sum / count : c.ambient;
        if (!count) rackMax = c.ambient;

        std::cout << std::fixed << std::setprecision(4)
                  << "{\"average_rack_temp\":" << avg
                  << ",\"max_rack_temp\":" << rackMax
                  << ",\"min_temp\":" << minT
                  << ",\"max_temp\":" << maxT
                  << ",\"steps\":" << steps
                  << ",\"dt_requested\":" << dtRequested
                  << ",\"dt_used\":" << dt
                  << ",\"nx\":" << nx << ",\"ny\":" << ny
                  << ",\"heatmap_file\":\"" << jsonEscape(outPath) << "\"}\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "thermal_sim error: " << e.what() << "\n";
        return 1;
    }
}