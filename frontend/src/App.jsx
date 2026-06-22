import React, { useState, useEffect, useRef } from 'react';
import axios from 'axios';
import ReactMarkdown from 'react-markdown';
import { 
  Send, MessageSquare, HeartPulse, LogOut, Search, Stethoscope, 
  Activity, ClipboardList, Plus, UserCheck, 
  AlertCircle, Eye, EyeOff, ArrowLeft, MoreVertical, Trash2
} from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import './App.css';

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

// Session-only storage: cleared when the browser tab is closed
const SESSION_TOKEN_KEY = 'medveda_token';
const SESSION_DOCTOR_KEY = 'medveda_doctor';

const clearAuthSession = () => {
  sessionStorage.removeItem(SESSION_TOKEN_KEY);
  sessionStorage.removeItem(SESSION_DOCTOR_KEY);
  localStorage.removeItem(SESSION_TOKEN_KEY);
  localStorage.removeItem(SESSION_DOCTOR_KEY);
};

const App = () => {

  // Authentication & Session States (tab session only — not restored after close)
  const [token, setToken] = useState(() => sessionStorage.getItem(SESSION_TOKEN_KEY));
  const [doctor, setDoctor] = useState(() => {
    const saved = sessionStorage.getItem(SESSION_DOCTOR_KEY);
    return saved ? JSON.parse(saved) : null;
  });

  // Doctor Auth Form States
  const [isRegisterMode, setIsRegisterMode] = useState(false);
  const [authEmail, setAuthEmail] = useState('');
  const [authPassword, setAuthPassword] = useState('');
  const [authName, setAuthName] = useState('');
  const [authSpecialty, setAuthSpecialty] = useState('Normal patients checking doctor');
  const [authLicense, setAuthLicense] = useState('');
  const [authHospital, setAuthHospital] = useState('My Clinic');
  const [authError, setAuthError] = useState(null);
  const [authLoading, setAuthLoading] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [showLogoutModal, setShowLogoutModal] = useState(false);
  const [registrationSuccess, setRegistrationSuccess] = useState(false);

  // Dashboard & Patient Queue States
  const [patients, setPatients] = useState([]);
  const [searchQuery, setSearchQuery] = useState('');
  const [isLoadingPatients, setIsLoadingPatients] = useState(false);
  const [activePatient, setActivePatient] = useState(null);
  const [backendStatus, setBackendStatus] = useState('online');
  const [openMenuChatId, setOpenMenuChatId] = useState(null);

  // AI Chat States
  const [doctorChats, setDoctorChats] = useState([]);
  const [activeChatId, setActiveChatId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [chatInput, setChatInput] = useState('');
  const [isSendingMessage, setIsSendingMessage] = useState(false);
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);

  const messagesEndRef = useRef(null);

  // Drop legacy persistent login from older builds
  useEffect(() => {
    localStorage.removeItem(SESSION_TOKEN_KEY);
    localStorage.removeItem(SESSION_DOCTOR_KEY);
  }, []);

  useEffect(() => {
    if (token) {
      sessionStorage.setItem(SESSION_TOKEN_KEY, token);
    } else {
      sessionStorage.removeItem(SESSION_TOKEN_KEY);
    }
  }, [token]);

  useEffect(() => {
    if (doctor) {
      sessionStorage.setItem(SESSION_DOCTOR_KEY, JSON.stringify(doctor));
    } else {
      sessionStorage.removeItem(SESSION_DOCTOR_KEY);
    }
  }, [doctor]);

  // Fetch Patients Queue & Doctor Chats
  const fetchQueueAndChats = async () => {
    if (!token) return;
    setIsLoadingPatients(true);
    try {
      const headers = { Authorization: `Bearer ${token}` };
      
      // Fetch Patients
      const patientRes = await axios.get(`${API_URL}/patients`, { headers });
      setPatients(patientRes.data);

      // Fetch Chats
      const chatRes = await axios.get(`${API_URL}/chats`, { headers });
      setDoctorChats(chatRes.data);
      
      setBackendStatus('online');
    } catch (err) {
      console.error('Failed to load portal data:', err);
      if (err.response?.status === 401) {
        handleLogout();
      } else {
        setBackendStatus('offline');
      }
    } finally {
      setIsLoadingPatients(false);
    }
  };

  useEffect(() => {
    if (token) {
      fetchQueueAndChats();
    }
  }, [token]);

  // Scroll to bottom of chat
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // Load chat session if activeChatId changes
  useEffect(() => {
    const loadChatSession = async () => {
      if (!activeChatId || !token) return;
      setIsLoadingHistory(true);
      try {
        const res = await axios.get(`${API_URL}/chats/${activeChatId}`, {
          headers: { Authorization: `Bearer ${token}` }
        });
        const loadedMsgs = res.data.messages.map(m => ({
          role: m.role,
          text: m.content
        }));
        setMessages(loadedMsgs);
      } catch (err) {
        console.error('Failed to fetch chat history:', err);
      } finally {
        setIsLoadingHistory(false);
      }
    };
    loadChatSession();
  }, [activeChatId]);

  // Handle Doctor Login
  const handleLogin = async (e) => {
    e.preventDefault();
    if (!authEmail || !authPassword) {
      setAuthError('Please enter email and password.');
      return;
    }
    setAuthLoading(true);
    setAuthError(null);

    try {
      const res = await axios.post(`${API_URL}/auth/doctor/login`, {
        email: authEmail,
        password: authPassword
      });
      setToken(res.data.access_token);
      setDoctor(res.data.user);
      setAuthPassword('');
    } catch (err) {
      console.error('Login error:', err);
      setAuthError(err.response?.data?.detail || 'Invalid email or password.');
    } finally {
      setAuthLoading(false);
    }
  };

  // Handle Doctor Registration
  const handleRegister = async (e) => {
    e.preventDefault();
    if (!authName || !authEmail || !authPassword || !authLicense) {
      setAuthError('Please fill in all required fields.');
      return;
    }
    setAuthLoading(true);
    setAuthError(null);

    try {
      await axios.post(`${API_URL}/auth/doctor/register`, {
        full_name: authName,
        email: authEmail,
        password: authPassword,
        specialty: authSpecialty,
        license_number: authLicense,
        hospital_name: authHospital
      });

      // Show success popup then redirect to login with all fields cleared
      setRegistrationSuccess(true);
      setTimeout(() => {
        setRegistrationSuccess(false);
        setAuthName('');
        setAuthEmail('');
        setAuthPassword('');
        setAuthSpecialty('Normal patients checking doctor');
        setAuthLicense('');
        setAuthHospital('My Clinic');
        setAuthError(null);
        setShowPassword(false);
        setIsRegisterMode(false);
      }, 2000);
    } catch (err) {
      console.error('Registration error:', err);
      setAuthError(err.response?.data?.detail || 'Registration failed. Email may already be in use.');
    } finally {
      setAuthLoading(false);
    }
  };

  const resetAuthForm = () => {
    setAuthEmail('');
    setAuthPassword('');
    setAuthName('');
    setAuthSpecialty('Normal patients checking doctor');
    setAuthLicense('');
    setAuthHospital('My Clinic');
    setAuthError(null);
    setShowPassword(false);
    setIsRegisterMode(false);
    setRegistrationSuccess(false);
  };

  // Handle Logout
  const handleLogout = () => {
    setToken(null);
    setDoctor(null);
    setActivePatient(null);
    setActiveChatId(null);
    setPatients([]);
    setDoctorChats([]);
    setMessages([]);
    setChatInput('');
    setSearchQuery('');
    setOpenMenuChatId(null);
    resetAuthForm();
    clearAuthSession();
  };

  // Send message to MedVeda Clinical AI
  const handleSendMessage = async (e) => {
    e?.preventDefault();
    if (!chatInput.trim() || !activePatient || isSendingMessage) return;

    const userMessageText = chatInput.trim();
    const newUserMessage = { role: 'user', text: userMessageText };
    
    setMessages(prev => [...prev, newUserMessage]);
    setChatInput('');
    setIsSendingMessage(true);

    try {
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      const res = await axios.post(`${API_URL}/chat`, {
        message: userMessageText,
        chat_id: activeChatId,
        patient_id: activePatient.patient_id
      }, { headers });

      const assistantMsg = { role: 'assistant', text: res.data.answer };
      setMessages(prev => [...prev, assistantMsg]);
      
      // If a new chat session was generated, update states and sync sidebar list
      if (!activeChatId) {
        setActiveChatId(res.data.chat_id);
        const chatListRes = await axios.get(`${API_URL}/chats`, { headers });
        setDoctorChats(chatListRes.data);
      }
    } catch (err) {
      console.error('Chat error:', err);
      setMessages(prev => [...prev, { role: 'assistant', text: '**Error:** Failed to connect to MedVeda AI server. Please try again.' }]);
    } finally {
      setIsSendingMessage(false);
    }
  };

  const startNewChat = () => {
    setActiveChatId(null);
    setMessages([]);
    setOpenMenuChatId(null);
  };

  const openPatientWorkspace = (patient) => {
    setActivePatient(patient);
    startNewChat();
  };

  const handleDeleteChat = async (chatId, e) => {
    e?.stopPropagation();
    if (!token) return;
    try {
      await axios.delete(`${API_URL}/chats/${chatId}`, {
        headers: { Authorization: `Bearer ${token}` }
      });
      setDoctorChats(prev => prev.filter(c => c.id !== chatId));
      if (activeChatId === chatId) startNewChat();
      setOpenMenuChatId(null);
    } catch (err) {
      console.error('Failed to delete chat:', err);
    }
  };

  const parsePatientNum = (id) => parseInt(String(id).replace(/\D/g, ''), 10) || 0;

  const filteredPatients = patients
    .filter(p =>
      p.full_name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      p.patient_id.toLowerCase().includes(searchQuery.toLowerCase())
    )
    .sort((a, b) => parsePatientNum(a.patient_id) - parsePatientNum(b.patient_id));

  // Filter chats specifically for the active patient
  const activePatientChats = activePatient 
    ? doctorChats.filter(c => c.patient_id === activePatient.patient_id)
    : [];

  const isChatEmpty = messages.length === 0 && !isLoadingHistory;

  const renderChatInput = () => (
    <form
      onSubmit={handleSendMessage}
      className="w-full max-w-2xl flex items-center gap-3 bg-white border border-slate-200 rounded-3xl px-5 py-3 shadow-lg focus-within:border-sky-400 focus-within:ring-2 focus-within:ring-sky-100 transition-all"
    >
      <textarea
        rows={1}
        placeholder="Describe your health concern or ask a question..."
        value={chatInput}
        onChange={e => setChatInput(e.target.value)}
        onKeyDown={e => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSendMessage();
          }
        }}
        className="flex-1 bg-transparent border-none text-sm leading-normal focus:outline-none resize-none py-1 text-slate-900 placeholder:text-slate-400"
        style={{ minHeight: '24px', maxHeight: '120px' }}
      />
      <button
        type="submit"
        disabled={isSendingMessage || !chatInput.trim()}
        className="w-9 h-9 rounded-full bg-sky-500 hover:bg-sky-600 text-white flex items-center justify-center shrink-0 disabled:opacity-40 cursor-pointer transition-colors"
      >
        <Send size={16} />
      </button>
    </form>
  );

  return (
    <div className="min-h-screen w-full relative">
          
          {/* SECURE SECURE BLUR OVERLAY FOR LOGOUT */}
          <AnimatePresence>
            {showLogoutModal && (
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className="fixed inset-0 z-[100] flex items-center justify-center bg-slate-950/40 backdrop-blur-md"
              >
                <motion.div
                  initial={{ scale: 0.95, opacity: 0 }}
                  animate={{ scale: 1, opacity: 1 }}
                  exit={{ scale: 0.95, opacity: 0 }}
                  className="bg-white border border-slate-200 rounded-3xl p-8 shadow-2xl max-w-sm w-full mx-4 flex flex-col items-center text-center"
                >
                  <div className="w-14 h-14 bg-red-50 text-red-500 rounded-2xl flex items-center justify-center mb-4">
                    <LogOut size={28} />
                  </div>
                  <h3 className="text-lg font-bold text-slate-900">End Clinical Session?</h3>
                  <p className="text-xs text-slate-500 mt-2 mb-6">
                    You will be logged out of MedVeda AI portal. All active sessions will be securely stored.
                  </p>
                  <div className="flex gap-3 w-full">
                    <button
                      type="button"
                      onClick={() => setShowLogoutModal(false)}
                      className="flex-1 border border-slate-200 hover:bg-slate-50 text-slate-700 font-semibold py-3 rounded-xl text-xs transition-all cursor-pointer"
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      onClick={() => { handleLogout(); setShowLogoutModal(false); }}
                      className="flex-1 bg-red-500 hover:bg-red-600 text-white font-bold py-3 rounded-xl text-xs transition-all cursor-pointer active:scale-98"
                    >
                      Logout Session
                    </button>
                  </div>
                </motion.div>
              </motion.div>
            )}
          </AnimatePresence>

          {/* DOCTOR AUTHENTICATION / LANDING WALL */}
          {!token ? (
            <div className="min-h-screen w-full flex items-center justify-center bg-slate-50 text-slate-900 font-sans p-4 relative overflow-hidden">
              <div className="absolute inset-0 bg-[radial-gradient(#e2e8f0_1px,transparent_1px)] [background-size:16px_16px] opacity-70" />
              
              <motion.div 
                initial={{ opacity: 0, y: 30 }}
                animate={{ opacity: 1, y: 0 }}
                className="w-full max-w-[480px] bg-white border border-slate-200 rounded-3xl p-8 shadow-xl relative z-10"
              >
                <div className="flex flex-col items-center mb-6">
                  <div className="w-14 h-14 bg-cyan-50 border border-cyan-100 rounded-2xl flex items-center justify-center text-cyan-600 mb-4 shadow-sm">
                    <HeartPulse size={30} className="animate-pulse" />
                  </div>
                  <h1 className="text-3xl font-extrabold tracking-tight text-slate-900">MedVeda AI</h1>
                  <p className="text-xs text-slate-500 mt-1">Clinical AI Diagnostics & Triage Hub</p>
                </div>

                {/* Login/Register Toggle Header */}
                <div className="flex bg-slate-50 border border-slate-100 p-1.5 rounded-2xl mb-6">
                  <button 
                    onClick={() => { setIsRegisterMode(false); setAuthError(null); }}
                    className={`flex-1 py-2.5 text-xs font-bold rounded-xl transition-all cursor-pointer ${!isRegisterMode ? 'bg-white text-cyan-600 shadow-sm' : 'text-slate-500 hover:text-slate-800'}`}
                  >
                    Doctor Login
                  </button>
                  <button 
                    onClick={() => { setIsRegisterMode(true); setAuthError(null); }}
                    className={`flex-1 py-2.5 text-xs font-bold rounded-xl transition-all cursor-pointer ${isRegisterMode ? 'bg-white text-cyan-600 shadow-sm' : 'text-slate-500 hover:text-slate-800'}`}
                  >
                    Doctor Registration
                  </button>
                </div>

                <form onSubmit={isRegisterMode ? handleRegister : handleLogin} className="space-y-4" autoComplete="off">
                  
                  {/* Registration success popup inside form */}
                  <AnimatePresence>
                    {registrationSuccess && (
                      <motion.div 
                        initial={{ opacity: 0, y: -10 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0 }}
                        className="bg-emerald-50 border border-emerald-200 p-4 rounded-xl text-center"
                      >
                        <span className="text-xs font-bold text-emerald-700">🎉 Registration Successful! Redirecting to login...</span>
                      </motion.div>
                    )}
                  </AnimatePresence>

                  {isRegisterMode && (
                    <>
                      <div>
                        <label className="block text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-1">Full Name</label>
                        <input 
                          type="text" 
                          required
                          placeholder="Enter your full name"
                          value={authName}
                          onChange={e => setAuthName(e.target.value)}
                          className="w-full bg-slate-50 border border-slate-200 focus:border-cyan-500 focus:bg-white rounded-xl px-4 py-3 text-xs focus:outline-none transition-all text-slate-900"
                        />
                      </div>
                      
                      <div className="grid grid-cols-2 gap-4">
                        <div>
                          <label className="block text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-1">MCI License Number</label>
                          <input 
                            type="text" 
                            required
                            placeholder="Enter MCI number"
                            value={authLicense}
                            onChange={e => setAuthLicense(e.target.value)}
                            className="w-full bg-slate-50 border border-slate-200 focus:border-cyan-500 focus:bg-white rounded-xl px-4 py-3 text-xs focus:outline-none transition-all text-slate-900"
                          />
                        </div>
                        <div>
                          <label className="block text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-1">Hospital / Clinic</label>
                          <input 
                            type="text" 
                            required
                            placeholder="Enter hospital name"
                            value={authHospital}
                            onChange={e => setAuthHospital(e.target.value)}
                            className="w-full bg-slate-50 border border-slate-200 focus:border-cyan-500 focus:bg-white rounded-xl px-4 py-3 text-xs focus:outline-none transition-all text-slate-900"
                          />
                        </div>
                      </div>

                      <div>
                        <label className="block text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-1">Specialization Required</label>
                        <select
                          value={authSpecialty}
                          onChange={e => setAuthSpecialty(e.target.value)}
                          className="w-full bg-slate-50 border border-slate-200 focus:border-cyan-500 focus:bg-white rounded-xl px-4 py-3 text-xs focus:outline-none transition-all text-slate-900 cursor-pointer"
                        >
                          <option value="General Practitioner">🏥 General Practitioner (GP)</option>
                        </select>
                      </div>
                    </>
                  )}

                  <div>
                    <label className="block text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-1">Email Address</label>
                    <input 
                      type="email" 
                      required
                      placeholder="Enter your email"
                      value={authEmail}
                      onChange={e => setAuthEmail(e.target.value)}
                      autoComplete="off"
                      name="medveda-login-email"
                      className="w-full bg-slate-50 border border-slate-200 focus:border-cyan-500 focus:bg-white rounded-xl px-4 py-3 text-xs focus:outline-none transition-all text-slate-900"
                    />
                  </div>

                  <div>
                    <label className="block text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-1">Password</label>
                    <div className="relative">
                      <input 
                        type={showPassword ? "text" : "password"} 
                        required
                        placeholder="Enter your password"
                        value={authPassword}
                        onChange={e => setAuthPassword(e.target.value)}
                        autoComplete="new-password"
                        name="medveda-login-password"
                        className="w-full bg-slate-50 border border-slate-200 focus:border-cyan-500 focus:bg-white rounded-xl px-4 py-3 pr-10 text-xs focus:outline-none transition-all text-slate-900"
                      />
                      <button
                        type="button"
                        onClick={() => setShowPassword(!showPassword)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 focus:outline-none cursor-pointer"
                      >
                        {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                      </button>
                    </div>
                  </div>

                  {authError && (
                    <motion.div 
                      initial={{ opacity: 0 }}
                      animate={{ opacity: 1 }}
                      className="flex items-start gap-2 bg-red-50 border border-red-200 text-red-600 p-3.5 rounded-xl text-xs animate-fade-in"
                    >
                      <AlertCircle size={15} className="shrink-0 mt-0.5" />
                      <span>{authError}</span>
                    </motion.div>
                  )}

                  <button
                    type="submit"
                    disabled={authLoading}
                    className="w-full bg-cyan-500 hover:bg-cyan-600 text-white font-bold py-3.5 rounded-xl transition-all shadow-sm flex items-center justify-center gap-2 cursor-pointer disabled:opacity-55"
                  >
                    {authLoading ? (
                      <div className="w-5 h-5 border-2 border-white border-t-transparent rounded-full animate-spin" />
                    ) : isRegisterMode ? 'Register Account' : 'Login Workspace'}
                  </button>
                </form>
              </motion.div>
            </div>
          ) : (
            
            /* DOCTOR MAIN PANEL (LOGGED IN WORKSPACE) */
            <div className="h-screen w-full flex bg-slate-50 text-slate-900 font-sans overflow-hidden">
              <AnimatePresence mode="wait">
                
                {/* CASE 1: NO ACTIVE PATIENT -> GENERAL CONTROL WORKSPACE */}
                {!activePatient ? (
                  <motion.div 
                    key="workstation"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    className="flex-1 flex flex-col h-full overflow-hidden px-6 py-5 md:px-10 md:py-6"
                  >
                    {/* Minimal top bar */}
                    <div className="flex items-center justify-between mb-5 shrink-0">
                      <div className="flex items-center gap-2">
                        <span className={`w-2 h-2 rounded-full ${backendStatus === 'online' ? 'bg-emerald-500' : 'bg-amber-400'}`} />
                        <span className="text-sm font-semibold text-slate-600">MedVeda-AI online</span>
                      </div>
                      <button
                        type="button"
                        onClick={() => setShowLogoutModal(true)}
                        className="flex items-center gap-1.5 text-sm font-semibold text-slate-500 hover:text-rose-600 transition-colors cursor-pointer"
                      >
                        <LogOut size={16} /> Logout
                      </button>
                    </div>

                    {/* Centered KPI cards */}
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6 shrink-0">
                      <div className="bg-white border border-slate-200/80 rounded-3xl p-6 shadow-sm flex flex-col items-center text-center">
                        <div className="w-14 h-14 rounded-2xl bg-sky-50 flex items-center justify-center text-sky-600 mb-3">
                          <Stethoscope size={26} strokeWidth={2} />
                        </div>
                        <h4 className="text-[11px] font-bold text-slate-400 uppercase tracking-wider mb-1">Clinician Profile</h4>
                        <p className="text-lg font-bold text-slate-900">{doctor.full_name}</p>
                        <span className="text-xs text-sky-700 bg-sky-50 font-semibold px-3 py-1 rounded-full mt-2">
                          {doctor.specialty}
                        </span>
                      </div>

                      <div className="bg-white border border-slate-200/80 rounded-3xl p-6 shadow-sm flex flex-col items-center text-center">
                        <div className="w-14 h-14 rounded-2xl bg-slate-50 flex items-center justify-center text-slate-600 mb-3">
                          <UserCheck size={26} strokeWidth={2} />
                        </div>
                        <h4 className="text-[11px] font-bold text-slate-400 uppercase tracking-wider mb-1">License Verification</h4>
                        <p className="text-base font-bold text-slate-800">MCI No: {doctor.license_number || 'N/A'}</p>
                        <p className="text-xs text-slate-500 mt-1">{doctor.hospital_name || 'City Hospital'}</p>
                      </div>

                      <div className="bg-white border border-slate-200/80 rounded-3xl p-6 shadow-sm flex flex-col items-center text-center">
                        <div className="w-14 h-14 rounded-2xl bg-emerald-50 flex items-center justify-center text-emerald-600 mb-3">
                          <Activity size={26} strokeWidth={2} />
                        </div>
                        <h4 className="text-[11px] font-bold text-slate-400 uppercase tracking-wider mb-1">Specialty Queue</h4>
                        <p className="text-base font-bold text-slate-800">
                          {patients.length} Active {patients.length === 1 ? 'Patient' : 'Patients'}
                        </p>
                        <p className="text-xs text-emerald-600 font-semibold mt-1">Smart filtered queue</p>
                      </div>
                    </div>

                    {/* Patient list */}
                    <div className="flex-1 bg-white border border-slate-200/80 rounded-3xl shadow-sm flex flex-col min-h-0 overflow-hidden">
                      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 px-5 py-4 border-b border-slate-100 shrink-0">
                        <div>
                          <h3 className="text-lg font-bold text-slate-900">Patient Queue Worklist</h3>
                          <p className="text-sm text-slate-500 mt-0.5">Open a workspace to start clinical consultation</p>
                        </div>
                        <div className="relative w-full sm:w-72">
                          <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={16} />
                          <input 
                            type="text" 
                            placeholder="Search by patient ID or name..." 
                            value={searchQuery}
                            onChange={e => setSearchQuery(e.target.value)}
                            className="w-full bg-slate-50 border border-slate-200 rounded-xl pl-10 pr-4 py-2.5 text-sm text-slate-900 focus:outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100 transition-all"
                          />
                        </div>
                      </div>

                      <div className="flex-1 overflow-y-auto custom-scrollbar">
                        {isLoadingPatients ? (
                          <div className="h-48 flex flex-col items-center justify-center text-slate-400">
                            <div className="w-8 h-8 border-2 border-sky-500 border-t-transparent rounded-full animate-spin mb-3" />
                            <span className="text-sm">Loading patient queue...</span>
                          </div>
                        ) : filteredPatients.length === 0 ? (
                          <div className="h-48 flex flex-col items-center justify-center text-slate-400">
                            <ClipboardList size={36} className="text-slate-300 mb-2" />
                            <p className="text-sm font-medium text-slate-600">No matching patients found</p>
                          </div>
                        ) : (
                          <div className="w-full px-5 md:px-6 py-2">
                            <div className="grid grid-cols-[minmax(72px,auto)_1fr_auto] items-center gap-4 text-[11px] font-bold text-slate-400 uppercase tracking-wider border-b border-slate-100 pb-3 mb-1">
                              <span>Patient ID</span>
                              <span>Name</span>
                              <span className="text-right">Action</span>
                            </div>
                            {filteredPatients.map(p => (
                              <div
                                key={p.patient_id}
                                className="grid grid-cols-[minmax(72px,auto)_1fr_auto] items-center gap-4 py-3.5 border-b border-slate-50 hover:bg-slate-50/80 transition-colors"
                              >
                                <span className="text-sm font-semibold text-sky-700">{p.patient_id}</span>
                                <span className="text-sm font-medium text-slate-800 truncate">{p.full_name}</span>
                                <button
                                  type="button"
                                  onClick={() => openPatientWorkspace(p)}
                                  className="justify-self-end px-4 py-2 text-sm font-semibold text-white bg-sky-500 hover:bg-sky-600 rounded-xl transition-colors cursor-pointer shadow-sm whitespace-nowrap"
                                >
                                  Open Workspace
                                </button>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  </motion.div>
                ) : (
                  
                  /* Patient clinical chat workspace */
                  <motion.div 
                    key="chat-workspace"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    className="flex-1 flex h-full overflow-hidden bg-white"
                    onClick={() => setOpenMenuChatId(null)}
                  >
                    <aside className="w-[260px] border-r border-slate-200 bg-slate-50 flex flex-col shrink-0 h-full">
                      <div className="p-4 flex items-center gap-2.5">
                        <div className="w-9 h-9 rounded-lg bg-sky-500 flex items-center justify-center text-white shadow-sm">
                          <HeartPulse size={20} />
                        </div>
                        <span className="text-base font-bold text-slate-900">MedVeda AI</span>
                      </div>

                      <div className="px-3 pb-3">
                        <button
                          type="button"
                          onClick={startNewChat}
                          className="w-full flex items-center justify-center gap-2 py-2.5 rounded-full border border-slate-200 bg-white hover:bg-slate-100 text-sm font-semibold text-slate-700 transition-colors cursor-pointer"
                        >
                          <Plus size={16} /> New chat
                        </button>
                      </div>

                      <div className="px-3 pb-3">
                        <div className="bg-white border border-slate-200 rounded-xl px-3 py-3 space-y-2.5 shadow-sm">
                          <div>
                            <p className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">Active patient</p>
                            <p className="text-sm font-bold text-slate-900 truncate mt-0.5">{activePatient.full_name}</p>
                            <p className="text-xs font-semibold text-sky-600">{activePatient.patient_id}</p>
                          </div>
                          <div className="grid grid-cols-2 gap-2 border-t border-slate-50 pt-2">
                             <div>
                               <p className="text-[9px] font-bold text-slate-400 uppercase">Age/Gender</p>
                               <p className="text-[11px] font-medium text-slate-700">{activePatient.age}y / {activePatient.gender}</p>
                             </div>
                             <div>
                               <p className="text-[9px] font-bold text-slate-400 uppercase">Blood Group</p>
                               <p className="text-[11px] font-medium text-slate-700">{activePatient.blood_group || 'N/A'}</p>
                             </div>
                             <div>
                               <p className="text-[9px] font-bold text-slate-400 uppercase">Visit Date</p>
                               <p className="text-[11px] font-medium text-slate-700">{activePatient.visit_date || 'N/A'}</p>
                             </div>
                             <div>
                               <p className="text-[9px] font-bold text-slate-400 uppercase">Visit Type</p>
                               <p className="text-[11px] font-medium text-slate-700">{activePatient.visit_type || 'N/A'}</p>
                             </div>
                          </div>
                          <div>
                            <p className="text-[9px] font-bold text-slate-400 uppercase">Contact</p>
                            <p className="text-[11px] font-medium text-slate-700">{activePatient.contact_no || 'N/A'}</p>
                          </div>
                        </div>
                      </div>

                      <div className="flex-1 overflow-y-auto custom-scrollbar px-2">
                        <p className="text-[10px] font-bold text-slate-400 uppercase tracking-wider px-2 mb-2">History</p>
                        {activePatientChats.length === 0 ? (
                          <p className="text-xs text-slate-400 px-2 py-4 text-center">No chats yet</p>
                        ) : (
                          activePatientChats.map(c => (
                            <div key={c.id} className="relative group mb-0.5">
                              <button
                                type="button"
                                onClick={() => { setActiveChatId(c.id); setOpenMenuChatId(null); }}
                                className={`w-full text-left px-3 py-2.5 pr-9 rounded-lg text-sm transition-colors truncate ${
                                  activeChatId === c.id
                                    ? 'bg-sky-100 text-sky-800 font-semibold'
                                    : 'text-slate-600 hover:bg-slate-200/60'
                                }`}
                              >
                                {c.title || 'New chat'}
                              </button>
                              <button
                                type="button"
                                onClick={(e) => { e.stopPropagation(); setOpenMenuChatId(openMenuChatId === c.id ? null : c.id); }}
                                className="absolute right-1 top-1/2 -translate-y-1/2 p-1 rounded-md text-slate-400 hover:text-slate-600 hover:bg-white opacity-0 group-hover:opacity-100 transition-opacity cursor-pointer"
                              >
                                <MoreVertical size={14} />
                              </button>
                              {openMenuChatId === c.id && (
                                <div className="absolute right-0 top-full mt-1 z-20 bg-white border border-slate-200 rounded-lg shadow-lg py-1 min-w-[120px]">
                                  <button
                                    type="button"
                                    onClick={(e) => handleDeleteChat(c.id, e)}
                                    className="w-full flex items-center gap-2 px-3 py-2 text-sm text-rose-600 hover:bg-rose-50 cursor-pointer"
                                  >
                                    <Trash2 size={14} /> Delete
                                  </button>
                                </div>
                              )}
                            </div>
                          ))
                        )}
                      </div>

                      <div className="p-3 border-t border-slate-200">
                        <button
                          type="button"
                          onClick={() => { setActivePatient(null); startNewChat(); }}
                          className="w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-sm font-medium text-slate-600 hover:bg-slate-200/70 transition-colors cursor-pointer"
                        >
                          <ArrowLeft size={18} />
                          <span>Patient worklist</span>
                        </button>
                      </div>
                    </aside>

                    <main className="flex-1 flex flex-col min-w-0 bg-white relative">
                      <div className="absolute top-4 left-5 flex items-center gap-2 z-10">
                        <span className="w-2 h-2 rounded-full bg-emerald-500" />
                        <span className="text-sm text-slate-500">MedVeda Online</span>
                      </div>

                      {isChatEmpty ? (
                        <div className="flex-1 flex flex-col items-center justify-center px-6">
                          <div className="w-[72px] h-[72px] rounded-2xl bg-sky-500 flex items-center justify-center text-white mb-8 shadow-md">
                            <HeartPulse size={36} />
                          </div>
                          <h1 className="text-3xl font-bold text-slate-900 text-center tracking-tight mb-8">
                            What can I help you with?
                          </h1>
                          <div className="w-full flex justify-center px-4">
                            {renderChatInput()}
                          </div>
                        </div>
                      ) : (
                        <>
                          <div className="flex-1 overflow-y-auto custom-scrollbar pt-14 pb-4 px-4 md:px-8">
                            {isLoadingHistory ? (
                              <div className="h-full flex items-center justify-center">
                                <div className="w-8 h-8 border-2 border-sky-500 border-t-transparent rounded-full animate-spin" />
                              </div>
                            ) : (
                              <div className="max-w-3xl mx-auto space-y-6">
                                {messages.map((msg, idx) => (
                                  <div key={idx} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                                    <div className={`max-w-[85%] px-4 py-3 rounded-2xl text-sm leading-relaxed ${
                                      msg.role === 'user'
                                        ? 'bg-sky-500 text-white rounded-br-md'
                                        : 'bg-slate-100 text-slate-800 rounded-bl-md markdown-body'
                                    }`}>
                                      {msg.role === 'user' ? msg.text : <ReactMarkdown>{msg.text}</ReactMarkdown>}
                                    </div>
                                  </div>
                                ))}
                                {isSendingMessage && (
                                  <div className="flex justify-start">
                                    <div className="flex gap-1.5 bg-slate-100 px-4 py-3 rounded-2xl">
                                      <span className="w-2 h-2 bg-sky-400 rounded-full animate-bounce" />
                                      <span className="w-2 h-2 bg-sky-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                                      <span className="w-2 h-2 bg-sky-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
                                    </div>
                                  </div>
                                )}
                                <div ref={messagesEndRef} />
                              </div>
                            )}
                          </div>
                          <div className="shrink-0 px-4 pb-6 flex justify-center">
                            {renderChatInput()}
                          </div>
                        </>
                      )}
                    </main>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          )}
        </div>
  );
};

export default App;
